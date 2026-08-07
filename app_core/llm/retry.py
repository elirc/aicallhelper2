"""The shared retry policy every answer provider uses.

Retry exactly once, and ONLY when the initial attempt failed at the
connection level BEFORE any delta reached the UI:
- never retry an HTTP error status (the server heard us and said no — an
  instant retry burns the first-token budget),
- never after a delta (the UI appends deltas; a second attempt would
  concatenate two answers),
- never after an abort (CancelledError is control flow, not failure).

The retried request is byte-identical: the ProviderRequest built once in
build_request is reused as-is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from app_core.llm.base import AnswerProvider, ProviderRequest


async def stream_answer(
    provider: AnswerProvider,
    request: ProviderRequest,
    http: httpx.AsyncClient,
    on_delta: Callable[[str], None],
) -> str:
    """Stream the answer, applying the retry-once policy. Returns the full answer text.

    The full answer is the concatenation of every delta with nothing between
    — it must equal what streamed into the panel byte for byte.
    """
    attempt = 0
    while True:
        attempt += 1
        got_delta = False
        parts: list[str] = []
        try:
            async for delta in provider.stream(request, http):
                got_delta = True
                parts.append(delta)
                on_delta(delta)
            return "".join(parts)
        except asyncio.CancelledError:
            raise  # abort is control flow — never retried, never remapped
        except Exception as exc:
            if attempt == 1 and not got_delta and provider.is_retryable(exc):
                continue
            raise
