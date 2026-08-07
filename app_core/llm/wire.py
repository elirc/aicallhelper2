"""Shared httpx streaming skeleton for SSE answer providers.

Kept importable (not module-private) deliberately: the Groq module doubles
as the template for any future OpenAI-compatible provider, and this is the
transport half of that template.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app_core.llm.base import ProviderFailure, ProviderRequest
from app_core.llm.sse import SSEParser

# The session machine owns the first-token (10 s) and total (60 s) timeouts by
# cancelling the stream task; these transport timeouts are only a backstop and
# must sit ABOVE the machine's timers so they never fire first.
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=10.0, pool=10.0)
BODY_SNIPPET_LEN = 400


async def stream_sse_data(
    request: ProviderRequest, http: httpx.AsyncClient
) -> AsyncIterator[str]:
    """POST the request, yield SSE data payloads; raise ProviderFailure on trouble.

    Failure kinds: "connect" (server never heard us — the only retryable
    kind), "status" (non-200), "stream_drop" (died after the response
    started), "empty_body" (200 that produced no SSE data at all).
    asyncio.CancelledError propagates untouched — abort is control flow.
    """
    response_started = False
    produced = False
    try:
        async with http.stream(
            "POST",
            request.url,
            headers=request.header_dict(),
            content=request.body,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response_started = True
            if response.status_code != 200:
                raw = await response.aread()
                snippet = raw[:BODY_SNIPPET_LEN].decode("utf-8", errors="replace")
                raise ProviderFailure(
                    "status", status=response.status_code, body_snippet=snippet
                )
            parser = SSEParser()
            async for chunk in response.aiter_bytes():
                for data in parser.feed(chunk):
                    produced = True
                    yield data
            for data in parser.flush():
                produced = True
                yield data
    except ProviderFailure:
        raise
    except httpx.HTTPError as exc:
        if response_started:
            raise ProviderFailure("stream_drop", detail=str(exc)) from exc
        if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
            # The request never left us: safe to retry.
            raise ProviderFailure("connect", detail=str(exc)) from exc
        # Anything else pre-response (notably ReadTimeout after the body was
        # written) means the server may have heard us — not retryable.
        raise ProviderFailure("timeout", detail=str(exc)) from exc
    if not produced:
        raise ProviderFailure("empty_body")
