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
            name, payload = await self._queue.get()
            while self._window is None:
                await asyncio.sleep(0.05)  # window not created yet — hold, don't drop
            detail = json.dumps({"name": name, "payload": payload}, ensure_ascii=False)
            # Double-encode so arbitrary payload text (quotes, backslashes,
            # newlines) survives the trip through evaluate_js as a JS string.
            code = (
                "window.dispatchEvent(new CustomEvent('app:event',"
                f"{{detail:JSON.parse({json.dumps(detail)})}}))"
            )
            # A dying webview must not kill the core loop.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._window.evaluate_js, code)
