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

import httpx

from app_core.errors import AppError
from app_core.llm.base import ProviderFailure, ProviderRequest
from app_core.llm.prompt import PromptParts
from app_core.llm.wire import stream_sse_data

GROQ_ORIGIN = "https://api.groq.com"
CHAT_COMPLETIONS_URL = GROQ_ORIGIN + "/openai/v1/chat/completions"
# Groq retires models on short notice; a 404 most likely means this constant
# needs updating (see classify below and the README note).
MODEL = "openai/gpt-oss-120b"
MAX_COMPLETION_TOKENS = 1024
TEMPERATURE = 0.7


def is_done_sentinel(data: str) -> bool:
    """`data: [DONE]` is a sentinel to SKIP, not a terminator — bytes after it
    in the same chunk still count."""
    return data.strip() == "[DONE]"


def extract_openai_delta(data: str) -> str | None:
    """Pull `choices[0].delta.content` out of one OpenAI-style SSE payload."""
    try:
        event = json.loads(data)
    except Exception:
        return None
    if not isinstance(event, dict):
        return None
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) and content else None


def classify_openai_failure(
    failure: ProviderFailure, *, provider_label: str, model_note: str
) -> AppError:
    """Shared OpenAI-style HTTP → AppError mapping. Reports the ACTUAL status —
    a 403 labelled 401 sends the user debugging the wrong thing."""
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
        if status == 404:
            return AppError("llm_http", model_note)
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
    ) -> AsyncIterator[str]:
        async for data in stream_sse_data(request, http):
            if is_done_sentinel(data):
                continue
            delta = extract_openai_delta(data)
            if delta is not None:
                yield delta

    def classify_error(self, failure: BaseException) -> AppError:
        if isinstance(failure, asyncio.CancelledError):
            return AppError("aborted", "Cancelled.")
        if isinstance(failure, ProviderFailure):
            return classify_openai_failure(
                failure,
                provider_label="Groq",
                model_note=(
                    "Groq returned 404 — the model may have been retired "
                    "(Groq retires models on short notice). Update the pinned "
                    "model constant in app_core/llm/groq.py."
                ),
            )
        return AppError("internal", "Unexpected error while generating the answer.")

    def is_retryable(self, failure: BaseException) -> bool:
        return isinstance(failure, ProviderFailure) and failure.kind == "connect"
