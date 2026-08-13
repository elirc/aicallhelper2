"""Core -> frontend event dispatch.

Events become a DOM CustomEvent named `app:event` with `{ name, payload }`.
Dispatch is serialized through ONE queue on the loop, so events for a
session arrive in the order the core emitted them. `evaluate_js` blocks on
the webview, so each dispatch runs in a worker thread — but strictly one at
a time, preserving order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

# Cap so one evaluate_js string stays small even if the queue backs up.
MAX_BATCH = 64


class WebviewEventSink:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
        self._window: Any = None
        self._task: asyncio.Task[None] | None = None

    def attach(self, window: Any) -> None:
        self._window = window

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._pump())

    def emit(self, name: str, payload: dict[str, object]) -> None:
        self._queue.put_nowait((name, payload))

    async def _pump(self) -> None:
        while True:
            first = await self._queue.get()
            while self._window is None:
                await asyncio.sleep(0.05)  # window not created yet — hold, don't drop
            # Drain whatever else is already queued into ONE evaluate_js.
            # Each call is a blocking round trip on a worker thread, and an
            # answer streams dozens of deltas per second — batching keeps
            # those hops off the latency budget. Order is preserved: the
            # queue is FIFO and the page dispatches the array in order.
            batch = [first]
            while len(batch) < MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            details = json.dumps(
                [{"name": name, "payload": payload} for name, payload in batch],
                ensure_ascii=False,
            )
            # Double-encode so arbitrary payload text (quotes, backslashes,
            # newlines, U+2028) survives the trip through evaluate_js as a JS
            # string: the outer dump escapes every non-ASCII character.
            code = (
                f"JSON.parse({json.dumps(details)}).forEach("
                "function(d){window.dispatchEvent("
                "new CustomEvent('app:event',{detail:d}))})"
            )
            # A dying webview must not kill the core loop.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._window.evaluate_js, code)
