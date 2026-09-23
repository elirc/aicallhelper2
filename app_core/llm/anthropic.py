"""Anthropic Claude — the default answer provider.

All model names, API versions, and base URLs live in the constants block
below. Prompt-caching logic is Anthropic-specific and lives entirely in this
module — it must not leak into the shared pipeline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app_core.errors import AppError
from app_core.llm.base import (
    Finish,
    ProviderFailure,
    ProviderRequest,
    StreamEnd,
    empty_answer_message,
)
from app_core.llm.prompt import PromptParts
from app_core.llm.wire import stream_sse_data

if TYPE_CHECKING:
    import httpx

ANTHROPIC_ORIGIN = "https://api.anthropic.com"
MESSAGES_URL = ANTHROPIC_ORIGIN + "/v1/messages"
MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"
# Spoken answers are short; an uncapped completion is pure tail latency.
MAX_TOKENS = 1024


# stop_reason values that mean the answer did NOT end where the model wanted.
_TRUNCATING_STOPS = frozenset({"max_tokens", "model_context_window_exceeded"})
_REFUSAL_STOPS = frozenset({"refusal"})
PROVIDER_MESSAGE_LEN = 200


def _parse_event(data: str) -> dict[str, object] | None:
    try:
        event = json.loads(data)
    except Exception:
        return None
    return event if isinstance(event, dict) else None


def _text_of(event: dict[str, object]) -> str | None:
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def extract_anthropic_delta(data: str) -> str | None:
    """Pull the text delta out of one SSE data payload, or None if it isn't one."""
    event = _parse_event(data)
    return _text_of(event) if event is not None else None


def finish_for_stop_reason(stop_reason: str | None) -> Finish:
    if stop_reason in _TRUNCATING_STOPS:
        return "truncated"
    if stop_reason in _REFUSAL_STOPS:
        return "refused"
    return "complete"


def _stream_error(event: dict[str, object]) -> ProviderFailure:
    """An in-stream `{"type":"error","error":{"type":..,"message":..}}` event."""
    err = event.get("error")
    err_type = ""
    message = ""
    if isinstance(err, dict):
        raw_type, raw_message = err.get("type"), err.get("message")
        err_type = raw_type if isinstance(raw_type, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""
    return ProviderFailure(
        "provider_error", detail=err_type, body_snippet=message[:PROVIDER_MESSAGE_LEN]
    )


class AnthropicProvider:
    id = "anthropic"
    display_name = "Claude Haiku 4.5 (recommended)"
    origin = ANTHROPIC_ORIGIN

    def build_request(self, prompt: PromptParts, api_key: str) -> ProviderRequest:
        # System prompt is TWO blocks with the cache breakpoint after the
        # resume+JD block, so a style flip never invalidates the cached
        # profile. Honesty note: Haiku's minimum cacheable prefix is 4096
        # tokens, so a typical 1-2K-token profile makes this marker a silent
        # no-op; it starts paying at roughly 16K+ characters of profile
        # (writes 1.25x, reads 0.1x, 5-minute TTL).
        # `usage.cache_read_input_tokens` in the response tells the truth
        # about whether it engaged.
        body = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "stream": True,
            "system": [
                {
                    "type": "text",
                    "text": prompt.cached_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": prompt.style_suffix},
            ],
            "messages": [{"role": "user", "content": prompt.user_message}],
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = (
            ("x-api-key", api_key),
            ("anthropic-version", API_VERSION),
            ("content-type", "application/json"),
        )
        return ProviderRequest(url=MESSAGES_URL, headers=headers, body=payload)

    async def stream(
        self, request: ProviderRequest, http: httpx.AsyncClient
    ) -> AsyncIterator[str | StreamEnd]:
        # The final answer is the concatenation of ALL text deltas across ALL
        # content blocks, joined with nothing between blocks (retry.py joins).
        # Completion is `message_stop`, never HTTP 200 or EOF: Anthropic
        # reports failures INSIDE a 200 stream as an `error` event, and a
        # stream cut short can end cleanly on a flushed partial line.
        # `message_delta` carries the stop_reason that says whether the
        # answer hit MAX_TOKENS. Unknown event types (ping, future ones) are
        # ignored.
        stop_reason: str | None = None
        async for data in stream_sse_data(request, http):
            event = _parse_event(data)
            if event is None:
                continue
            kind = event.get("type")
            if kind == "error":
                raise _stream_error(event)
            if kind == "message_delta":
                delta_obj = event.get("delta")
                if isinstance(delta_obj, dict):
                    raw = delta_obj.get("stop_reason")
                    if isinstance(raw, str):
                        stop_reason = raw
                continue
            if kind == "message_stop":
                yield StreamEnd(finish_for_stop_reason(stop_reason), stop_reason)
                return
            text = _text_of(event)
            if text is not None:
                yield text
        raise ProviderFailure("incomplete", detail="stream ended before message_stop")

    def classify_error(self, failure: BaseException) -> AppError:
        # Abort is checked FIRST — it must never surface as a scary HTTP error.
        if isinstance(failure, asyncio.CancelledError):
            return AppError("aborted", "Cancelled.")
        if isinstance(failure, ProviderFailure):
            if failure.kind == "status":
                status = failure.status or 0
                if status == 401:
                    return AppError(
                        "llm_auth",
                        "Anthropic rejected the API key (401). Check it in Settings.",
                    )
                if status == 403:
                    return AppError(
                        "llm_auth",
                        "Anthropic refused the request (403): this API key is not "
                        "allowed to use the model. Check the key in Settings.",
                    )
                if status == 429:
                    return AppError(
                        "llm_rate_limit",
                        "Anthropic rate limit hit (429). Wait a moment and try "
                        "again, and check your credit balance.",
                    )
                if status == 529:
                    return AppError(
                        "llm_http", "Anthropic is overloaded (529). Try again in a moment."
                    )
                snippet = f" {failure.body_snippet}".rstrip()
                return AppError("llm_http", f"Anthropic returned HTTP {status}.{snippet}")
            if failure.kind == "connect":
                return AppError(
                    "llm_http", "Could not reach Anthropic. Check your internet connection."
                )
            if failure.kind == "timeout":
                return AppError(
                    "llm_http",
                    "Anthropic did not respond in time. Check your connection and try again.",
                )
            if failure.kind == "stream_drop":
                return AppError(
                    "llm_http",
                    "The connection to Anthropic dropped while the answer was streaming.",
                )
            if failure.kind == "provider_error":
                if failure.detail == "overloaded_error":
                    return AppError(
                        "llm_http", "Anthropic is overloaded right now. Try again in a moment."
                    )
                if failure.detail == "rate_limit_error":
                    return AppError(
                        "llm_rate_limit",
                        "Anthropic rate limit hit. Wait a moment and try again, "
                        "and check your credit balance.",
                    )
                snippet = f" {failure.body_snippet}".rstrip()
                return AppError(
                    "llm_http", f"Anthropic reported an error during the answer.{snippet}"
                )
            if failure.kind == "incomplete":
                return AppError(
                    "llm_http",
                    "The answer stopped before Anthropic finished it (the stream "
                    "ended early). Try again.",
                )
            if failure.kind == "empty_answer":
                return AppError("llm_http", empty_answer_message("Anthropic", failure))
            return AppError("llm_http", "Anthropic returned an empty response. Try again.")
        return AppError("internal", "Unexpected error while generating the answer.")

    def is_retryable(self, failure: BaseException) -> bool:
        return isinstance(failure, ProviderFailure) and failure.kind == "connect"
