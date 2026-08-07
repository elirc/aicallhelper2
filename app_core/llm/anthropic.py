"""Anthropic Claude — the default answer provider.

All model names, API versions, and base URLs live in the constants block
below. Prompt-caching logic is Anthropic-specific and lives entirely in this
module — it must not leak into the shared pipeline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app_core.errors import AppError
from app_core.llm.base import ProviderFailure, ProviderRequest
from app_core.llm.prompt import PromptParts
from app_core.llm.wire import stream_sse_data

ANTHROPIC_ORIGIN = "https://api.anthropic.com"
MESSAGES_URL = ANTHROPIC_ORIGIN + "/v1/messages"
MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"
# Spoken answers are short; an uncapped completion is pure tail latency.
MAX_TOKENS = 1024


def extract_anthropic_delta(data: str) -> str | None:
    """Pull the text delta out of one SSE data payload, or None if it isn't one."""
    try:
        event = json.loads(data)
    except Exception:
        return None
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


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
    ) -> AsyncIterator[str]:
        # The final answer is the concatenation of ALL text deltas across ALL
        # content blocks, joined with nothing between blocks (retry.py joins).
        async for data in stream_sse_data(request, http):
            delta = extract_anthropic_delta(data)
            if delta is not None:
                yield delta

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
            return AppError("llm_http", "Anthropic returned an empty response. Try again.")
        return AppError("internal", "Unexpected error while generating the answer.")

    def is_retryable(self, failure: BaseException) -> bool:
        return isinstance(failure, ProviderFailure) and failure.kind == "connect"
