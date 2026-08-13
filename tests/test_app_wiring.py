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


class SlowAffinity:
    """Refuses until `honor_after` calls, mimicking an HWND that is not ready."""

    def __init__(self, honor_after: int) -> None:
        self.honor_after = honor_after
        self.calls = 0
        self.stored = 0

    def set_affinity(self, hwnd: int, affinity: int) -> bool:
        self.calls += 1
        if self.calls > self.honor_after:
            self.stored = affinity
            return True
        return False

    def get_affinity(self, hwnd: int) -> int | None:
        return self.stored


class TestContentProtectionVerdict:
    """`shown` and `loaded` each start an attempt, so they overlap. A slow
    loser must never overwrite a fresher verdict: telling the user the window
    is hidden from screen capture when the latest attempt failed is the worst
    outcome this app has."""

    def test_a_stale_attempt_cannot_overwrite_a_fresher_verdict(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        emitted: list[str] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda name, payload: emitted.append(name)
        )
        monkeypatch.setattr(application, "_hwnd", lambda: 0x1234)
        monkeypatch.setattr(app_module, "CONTENT_PROTECTION_RETRY_S", 0.01)
        # Never succeeds — this is the "loser" that will finish last.
        application._affinity_api = SlowAffinity(honor_after=99)

        import threading as _threading

        started = _threading.Event()
        original = application._apply_content_protection

        def slow_attempt() -> None:
            started.set()
            original()

        loser = _threading.Thread(target=slow_attempt, daemon=True)
        loser.start()
        started.wait(timeout=2.0)
        # A newer attempt claims a higher ticket and succeeds immediately.
        application._affinity_api = SlowAffinity(honor_after=0)
        application._apply_content_protection()
        loser.join(timeout=5.0)

        assert emitted, "no verdict was reported at all"
        assert emitted[-1] == "protection:ok", emitted
        assert application._protection_failed is False
        assert emitted.count("protection:failed") == 0, (
            "the superseded attempt reported anyway: " + repr(emitted)
        )

    def test_a_verified_failure_is_reported(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        emitted: list[str] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda name, payload: emitted.append(name)
        )
        monkeypatch.setattr(application, "_hwnd", lambda: 0x1234)
        monkeypatch.setattr(app_module, "CONTENT_PROTECTION_RETRY_S", 0.001)
        application._affinity_api = SlowAffinity(honor_after=99)
        application._apply_content_protection()
        assert emitted == ["protection:failed"]
        assert application._protection_failed is True

    def test_a_late_ready_hwnd_still_succeeds_via_retries(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        emitted: list[str] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda name, payload: emitted.append(name)
        )
        monkeypatch.setattr(application, "_hwnd", lambda: 0x1234)
        monkeypatch.setattr(app_module, "CONTENT_PROTECTION_RETRY_S", 0.001)
        application._affinity_api = SlowAffinity(honor_after=2)  # ready on attempt 3
        application._apply_content_protection()
        assert emitted == ["protection:ok"]


class TestMinimizedGeometry:
    def test_minimized_window_bounds_are_not_persisted(self) -> None:
        import app as app_module

        # Windows parks minimized windows at (-32000, -32000) with a
        # titlebar-sized rect; saving it loses the geometry the user arranged.
        assert not app_module.plausible_bounds(
            {"x": -32000, "y": -32000, "width": 237, "height": 39}
        )
        assert not app_module.plausible_bounds(
            {"x": 100, "y": 100, "width": 10, "height": 10}
        )
        # Real geometry, including a monitor left of the primary, still saves.
        assert app_module.plausible_bounds({"x": 100, "y": 100, "width": 460, "height": 700})
        assert app_module.plausible_bounds({"x": -1500, "y": 40, "width": 460, "height": 700})
