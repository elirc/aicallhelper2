"""Pre-warming the answer provider's origin.

Fired on Record press, on Ask, and on Stop press (before awaiting the stop):
an unauthenticated GET <origin>/v1/models through the SHARED httpx client,
reading the body to completion so the connection returns to the pool. This
is why the answer request after Stop finds a live pooled TLS connection
instead of paying the handshake inside the stop-to-first-word window.

A failed warm costs nothing and must never raise or log loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

import httpx

WARM_TIMEOUT_S = 3.0
WARM_THROTTLE_S = 2.0


class PreWarmer:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        throttle_s: float = WARM_THROTTLE_S,
    ) -> None:
        self._http = http
        self._clock = clock
        self._throttle_s = throttle_s
        self._last_warm: dict[str, float] = {}

    def warm(self, origin: str) -> asyncio.Task[None] | None:
        """Fire-and-forget warm, throttled to one per origin per 2 s.

        Returns the task (for tests) or None when throttled.
        """
        now = self._clock()
        last = self._last_warm.get(origin)
        if last is not None and now - last < self._throttle_s:
            return None
        self._last_warm[origin] = now
        return asyncio.get_running_loop().create_task(self._do_warm(origin))

    async def _do_warm(self, origin: str) -> None:
        # httpx .get reads the body to completion before returning, which is
        # exactly what returns the connection to the pool. A failed warm is
        # free — never raise, never log loudly.
        with contextlib.suppress(Exception):
            await self._http.get(origin + "/v1/models", timeout=WARM_TIMEOUT_S)
