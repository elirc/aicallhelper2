"""Groq — the user-selectable "fastest" preset.

This module doubles as the TEMPLATE for any future OpenAI-compatible
provider: `extract_openai_delta`, `is_done_sentinel`, and
`classify_openai_failure` are importable/reusable, and the transport half
lives in wire.py.

gpt-oss is a reasoning model and reasoning is the enemy of
time-to-first-word; `reasoning_effort: "low"` + `include_reasoning: false`
are the supported knobs for this family (`reasoning_format` is a Qwen-family
knob — do not send it).
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

GROQ_ORIGIN = "https://api.groq.com"
CHAT_COMPLETIONS_URL = GROQ_ORIGIN + "/openai/v1/chat/completions"
# Groq retires models on short notice; a 404 most likely means this constant
# needs updating (see classify below and the README note).
MODEL = "openai/gpt-oss-120b"
MAX_COMPLETION_TOKENS = 1024
TEMPERATURE = 0.7
PROVIDER_MESSAGE_LEN = 200


def is_done_sentinel(data: str) -> bool:
    """`data: [DONE]` is a sentinel to SKIP, not a terminator — bytes after it
    in the same chunk still count."""
    return data.strip() == "[DONE]"


def _parse_event(data: str) -> dict[str, object] | None:
    try:
        event = json.loads(data)
    except Exception:
        return None
    return event if isinstance(event, dict) else None


def _first_choice(event: dict[str, object]) -> dict[str, object] | None:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    return first if isinstance(first, dict) else None


def _content_of(event: dict[str, object]) -> str | None:
    first = _first_choice(event)
    if first is None:
        return None
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) and content else None


def extract_openai_delta(data: str) -> str | None:
    """Pull `choices[0].delta.content` out of one OpenAI-style SSE payload."""
    event = _parse_event(data)
    return _content_of(event) if event is not None else None


def extract_openai_finish_reason(data: str) -> str | None:
    """`choices[0].finish_reason` — null on every chunk but the last."""
    event = _parse_event(data)
    first = _first_choice(event) if event is not None else None
    reason = first.get("finish_reason") if first is not None else None
    return reason if isinstance(reason, str) and reason else None


def finish_for_openai_reason(reason: str | None) -> Finish:
    if reason == "length":
        return "truncated"
    if reason == "content_filter":
        return "refused"
    return "complete"


async def stream_openai_answer(
    request: ProviderRequest, http: httpx.AsyncClient
) -> AsyncIterator[str | StreamEnd]:
    """The OpenAI-style stream with provider-level completion checks.

    Completion = a non-null `finish_reason` or the `[DONE]` sentinel; a
    stream that ends with neither was cut short (the SSE parser flushes a
    truncated final line, so a clean EOF proves nothing). An `{"error": ..}`
    payload inside the 200 stream is a failure, not an ignorable chunk.
    """
    finish_reason: str | None = None
    seen_done = False
    async for data in stream_sse_data(request, http):
        if is_done_sentinel(data):
            seen_done = True
            continue
        event = _parse_event(data)
        if event is None:
            continue
        err = event.get("error")
        if err is not None:
            raise _openai_stream_error(err)
        content = _content_of(event)
        if content is not None:
            yield content
        first = _first_choice(event)
        reason = first.get("finish_reason") if first is not None else None
        if isinstance(reason, str) and reason:
            finish_reason = reason
    if finish_reason is None and not seen_done:
        raise ProviderFailure("incomplete", detail="stream ended without finish_reason")
    yield StreamEnd(finish_for_openai_reason(finish_reason), finish_reason)


def _openai_stream_error(err: object) -> ProviderFailure:
    err_type = ""
    message = ""
    if isinstance(err, dict):
        raw_type = err.get("code") or err.get("type")
        raw_message = err.get("message")
        err_type = raw_type if isinstance(raw_type, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""
    elif isinstance(err, str):
        message = err
    return ProviderFailure(
        "provider_error", detail=err_type, body_snippet=message[:PROVIDER_MESSAGE_LEN]
    )


_MODEL_GONE_MARKERS = ("model_not_found", "model_decommissioned", "does not exist")


def _is_model_gone(failure: ProviderFailure) -> bool:
    if failure.kind != "status":
        return False
    if failure.status == 404:
        return True
    body = failure.body_snippet.lower()
    return failure.status == 400 and any(m in body for m in _MODEL_GONE_MARKERS)


def classify_openai_failure(
    failure: ProviderFailure, *, provider_label: str, model_note: str
) -> AppError:
    """Shared OpenAI-style HTTP → AppError mapping. Reports the ACTUAL status —
    a 403 labelled 401 sends the user debugging the wrong thing."""
    if _is_model_gone(failure):
        return AppError("llm_http", model_note)
    if failure.kind == "status":
        status = failure.status or 0
        if status in (401, 403):
            return AppError(
                "llm_auth",
                f"{provider_label} rejected the API key ({status}). Check it in Settings.",
            )
        if status == 429:
            return AppError(
                "llm_rate_limit",
                f"{provider_label} rate limit hit (429). Wait a moment and try again.",
            )
        if status >= 500:
            return AppError(
                "llm_http",
                f"{provider_label} is unavailable ({status}). Try again in a moment.",
            )
        snippet = f" {failure.body_snippet}".rstrip()
        return AppError("llm_http", f"{provider_label} returned HTTP {status}.{snippet}")
    if failure.kind == "connect":
        return AppError(
            "llm_http", f"Could not reach {provider_label}. Check your internet connection."
        )
    if failure.kind == "timeout":
        return AppError(
            "llm_http",
            f"{provider_label} did not respond in time. Check your connection and try again.",
        )
    if failure.kind == "stream_drop":
        return AppError(
            "llm_http",
            f"The {provider_label} connection dropped while the answer was streaming.",
        )
    if failure.kind == "provider_error":
        if "rate_limit" in failure.detail:
            return AppError(
                "llm_rate_limit",
                f"{provider_label} rate limit hit. Wait a moment and try again.",
            )
        snippet = f" {failure.body_snippet}".rstrip()
        return AppError(
            "llm_http", f"{provider_label} reported an error during the answer.{snippet}"
        )
    if failure.kind == "incomplete":
        return AppError(
            "llm_http",
            f"The answer stopped before {provider_label} finished it (the stream "
            "ended early). Try again.",
        )
    if failure.kind == "empty_answer":
        return AppError("llm_http", empty_answer_message(provider_label, failure))
    return AppError(
        "llm_http", f"{provider_label} returned an empty response. Try again."
    )


class GroqProvider:
    id = "groq"
    display_name = "Groq GPT-OSS 120B (fastest)"
    origin = GROQ_ORIGIN

    def build_request(self, prompt: PromptParts, api_key: str) -> ProviderRequest:
        # OpenAI-style APIs take ONE system string: prefix + suffix joined.
        body = {
            "model": MODEL,
            "stream": True,
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "reasoning_effort": "low",
            "include_reasoning": False,
            "messages": [
                {
                    "role": "system",
                    "content": prompt.cached_prefix + "\n\n" + prompt.style_suffix,
                },
                {"role": "user", "content": prompt.user_message},
            ],
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = (
            ("Authorization", f"Bearer {api_key}"),
            ("content-type", "application/json"),
        )
        return ProviderRequest(url=CHAT_COMPLETIONS_URL, headers=headers, body=payload)

    async def stream(
        self, request: ProviderRequest, http: httpx.AsyncClient
    ) -> AsyncIterator[str | StreamEnd]:
        async for item in stream_openai_answer(request, http):
            yield item

    def classify_error(self, failure: BaseException) -> AppError:
        if isinstance(failure, asyncio.CancelledError):
            return AppError("aborted", "Cancelled.")
        if isinstance(failure, ProviderFailure):
            # Installed-app users cannot edit source: tell them what they CAN
            # do (switch provider, update the app). Developers: see MODEL.
            return classify_openai_failure(
                failure,
                provider_label="Groq",
                model_note=(
                    f"Groq no longer offers the model this version of the app "
                    f"uses ({MODEL}); Groq retires models on short notice. "
                    "Switch the answer provider to Claude in Settings, or "
                    "install the latest version of the app."
                ),
            )
        return AppError("internal", "Unexpected error while generating the answer.")

    def is_retryable(self, failure: BaseException) -> bool:
        return isinstance(failure, ProviderFailure) and failure.kind == "connect"
