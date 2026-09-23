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
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from app_core.llm.base import (
    AnswerProvider,
    AnswerResult,
    ProviderFailure,
    ProviderRequest,
    StreamEnd,
)

if TYPE_CHECKING:
    import httpx


async def stream_answer(
    provider: AnswerProvider,
    request: ProviderRequest,
    http: httpx.AsyncClient,
    on_delta: Callable[[str], None],
) -> AnswerResult:
    """Stream the answer, applying the retry-once policy.

    The full answer is the concatenation of every delta with nothing between
    — it must equal what streamed into the panel byte for byte. How it ended
    comes from the provider's StreamEnd (absent = "complete"). A finished
    stream with no non-whitespace text is a failure ("empty_answer"), never
    a blank success: the server heard us, so it is not retried either.
    """
    attempt = 0
    while True:
        attempt += 1
        got_delta = False
        parts: list[str] = []
        end = StreamEnd()
        try:
            items = provider.stream(request, http)
            try:
                async for item in items:
                    if isinstance(item, StreamEnd):
                        end = item
                        continue
                    got_delta = True
                    parts.append(item)
                    on_delta(item)
            finally:
                # If on_delta raises, close the provider's generator (and the
                # HTTP response it holds open) now, not whenever GC gets to it.
                aclose = getattr(items, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
            text = "".join(parts)
            if not text.strip():
                raise ProviderFailure(
                    "empty_answer", finish=end.finish, detail=end.stop_reason or ""
                )
            return AnswerResult(text=text, finish=end.finish, stop_reason=end.stop_reason)
        except asyncio.CancelledError:
            raise  # abort is control flow — never retried, never remapped
        except Exception as exc:
            if attempt == 1 and not got_delta and provider.is_retryable(exc):
                continue
            raise
