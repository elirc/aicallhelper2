"""The seam between the shell and the core.

Every other suite mocks one side of this boundary: core tests use a fake
event sink, bridge tests attach a fake window themselves, and the frontend
tests dispatch events by hand. That left the one line that connects them
untested — and it was missing for four commits, so the shipped app answered
no questions visibly: commands worked (js_api is its own channel) while the
event pump parked forever on `while self._window is None`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


class StubEventSlot:
    """pywebview exposes window callbacks as `window.events.X += handler`."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> StubEventSlot:
        self.handlers.append(handler)
        return self


class StubEvents:
    def __init__(self) -> None:
        self.shown = StubEventSlot()
        self.loaded = StubEventSlot()
        self.moved = StubEventSlot()
        self.resized = StubEventSlot()
        self.closing = StubEventSlot()


class StubWindow:
    def __init__(self) -> None:
        self.events = StubEvents()
        self.scripts: list[str] = []

    def evaluate_js(self, code: str) -> None:
        self.scripts.append(code)


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import app as app_module

    instance = app_module.App()
    yield instance
    instance.loop.close()


class TestWindowWiring:
    def test_wiring_attaches_the_event_sink(self, application: Any) -> None:
        window = StubWindow()
        application._wire_window(window)
        # Without this the core can emit all it likes and the page never hears
        # a transcript, an answer delta, an audio level, or an error.
        assert application.sink._window is window
        assert application.window is window

    def test_wiring_subscribes_every_window_callback(self, application: Any) -> None:
        window = StubWindow()
        application._wire_window(window)
        events = window.events
        assert len(events.shown.handlers) == 1, "content protection on show"
        assert len(events.loaded.handlers) == 1, "content protection after reload"
        assert len(events.moved.handlers) == 1, "debounced geometry save"
        assert len(events.resized.handlers) == 1, "debounced geometry save"
        assert len(events.closing.handlers) == 1, "geometry flush on close"

    async def test_an_emitted_event_actually_reaches_the_window(
        self, application: Any
    ) -> None:
        """End to end across the seam: sink.emit -> pump -> window.evaluate_js.

        Driven on this test's own loop rather than the app's background
        thread, which is what the pump only needs to be running somewhere.
        """
        from app_core.bridge.events import WebviewEventSink

        window = StubWindow()
        sink = WebviewEventSink()
        application.sink = sink
        application._wire_window(window)
        sink.start()
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "hello"})
        deadline = asyncio.get_running_loop().time() + 3.0
        while not window.scripts and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert window.scripts, "the pump never dispatched — is the sink attached?"
        assert "hello" in window.scripts[0]
        assert "app:event" in window.scripts[0]
