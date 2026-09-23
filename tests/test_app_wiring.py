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
import faulthandler
import sys
import threading
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


class ReloadableWindow(StubWindow):
    def __init__(self, *, alive: bool, hang: bool = False) -> None:
        super().__init__()
        self.alive = alive
        self.hang = hang
        self.loaded: list[str] = []
        self.probes = 0

    def evaluate_js(self, code: str) -> Any:
        self.probes += 1
        if self.hang:
            import threading as _threading

            _threading.Event().wait(0.5)  # a dead renderer never answers
            return None
        if not self.alive:
            raise RuntimeError("WebView2 process gone")
        return 1

    def load_url(self, url: str) -> None:
        self.loaded.append(url)


def booted(application: Any, window: Any) -> Any:
    """Wire a window whose page has finished loading (the watchdog only
    judges pages that have booted — see TestWatchdogBootGate)."""
    application._wire_window(window)
    application._page_loaded = True
    return window


class TestRendererWatchdog:
    """The heartbeat is a hint, not a verdict. Chromium throttles timers in a
    hidden page to once per minute after five minutes minimized, so a stale
    heartbeat with a healthy page is the NORMAL state of a minimized app —
    reloading on it would wipe the history every 25 s the window sat in the
    taskbar."""

    def test_a_fresh_heartbeat_never_probes(self, application: Any) -> None:
        window = booted(application, ReloadableWindow(alive=True))
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=100.0 + 5.0) is False
        assert window.probes == 0 and window.loaded == []

    def test_a_stale_heartbeat_from_a_throttled_live_page_is_forgiven(
        self, application: Any
    ) -> None:
        window = booted(application, ReloadableWindow(alive=True))
        application.api.last_heartbeat = 100.0
        now = 100.0 + 60.0
        assert application._check_renderer(now=now) is False
        assert window.probes == 1
        assert window.loaded == [], "a live page was reloaded"
        # Forgiven means the clock restarts: no probe storm every 2 s tick.
        assert application.api.last_heartbeat == now
        assert application._check_renderer(now=now + 2.0) is False
        assert window.probes == 1

    def test_a_dead_renderer_is_reloaded_once_per_cooldown(self, application: Any) -> None:
        window = booted(application, ReloadableWindow(alive=False))
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=130.0) is True
        assert window.loaded == [application._entry_url]
        # Still dead 2 s later: within the cooldown, no flicker.
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=132.0) is False
        assert len(window.loaded) == 1
        # The reloaded page loads, then dies too: past the cooldown and
        # stale, it is reloaded again.
        application._page_loaded = True
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=150.0) is True
        assert len(window.loaded) == 2

    def test_a_hung_renderer_counts_as_dead(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        monkeypatch.setattr(app_module, "RENDERER_PROBE_TIMEOUT_S", 0.05)
        window = booted(application, ReloadableWindow(alive=True, hang=True))
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=130.0) is True
        assert window.loaded == [application._entry_url]

    def test_no_window_yet_is_a_no_op(self, application: Any) -> None:
        application.api.last_heartbeat = 0.0
        assert application._check_renderer(now=1_000.0) is False


class TestWatchdogBootGate:
    """R11: resetting the heartbeat clock on shown/loaded cannot protect a
    page that is still booting — `last_heartbeat` was already set at API
    construction and the watchdog runs before `webview.start()`. A probe of
    a booting page blocks in pywebview's own wait for the page, the 3 s join
    expires, and the boot is misread as a death and reloaded."""

    def test_a_page_that_was_never_shown_is_never_judged(self, application: Any) -> None:
        window = ReloadableWindow(alive=False)
        application._wire_window(window)
        application.api.last_heartbeat = 0.0  # ancient, from construction
        assert application._check_renderer(now=10_000.0) is False
        assert window.probes == 0 and window.loaded == []

    def test_a_slow_booting_page_is_not_probed_or_reloaded(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = ReloadableWindow(alive=False)
        application._wire_window(window)
        monkeypatch.setattr(application, "_protect_async", lambda: None)
        application._on_shown()
        shown_at = application._boot_started
        application.api.last_heartbeat = shown_at - 100.0  # long stale
        # 30 s into the boot and no `loaded` yet.
        assert application._check_renderer(now=shown_at + 30.0) is False
        assert window.probes == 0 and window.loaded == []

    def test_a_boot_that_never_completes_is_recovered_after_the_grace(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        window = ReloadableWindow(alive=False)
        application._wire_window(window)
        monkeypatch.setattr(application, "_protect_async", lambda: None)
        application._on_shown()
        shown_at = application._boot_started
        now = shown_at + app_module.BOOT_GRACE_S + 1
        assert application._check_renderer(now=now) is True
        assert window.loaded == [application._entry_url]

    def test_a_reloaded_page_gets_a_fresh_boot_grace(self, application: Any) -> None:
        import app as app_module

        window = booted(application, ReloadableWindow(alive=False))
        application.api.last_heartbeat = 100.0
        generation = application.sink.page_generation
        assert application._check_renderer(now=130.0) is True
        # Advanced BEFORE load_url: events abandoned on the dead page carry
        # the old generation and the new page can drop them.
        assert application.sink.page_generation == generation + 1
        # Reloaded at 130; still no `loaded` 20 s later: it is booting, not
        # dead — the old code reloaded it again here, mid-boot.
        assert application._check_renderer(now=150.0) is False
        assert len(window.loaded) == 1
        assert application._check_renderer(now=130.0 + app_module.BOOT_GRACE_S + 1) is True

    def test_a_watchdog_reload_advances_the_generation_exactly_once(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pywebview holds evaluate_js until `loaded`, so a dispatch started in
        # the load window is stamped with the reload's generation and runs on
        # the NEW page. A second bump in `loaded` made that page (which reads
        # get_status after `loaded`) drop it — including terminal events.
        booted(application, ReloadableWindow(alive=False))
        monkeypatch.setattr(application, "_protect_async", lambda: None)
        application.api.last_heartbeat = 100.0
        generation = application.sink.page_generation
        assert application._check_renderer(now=130.0) is True
        application._on_loaded()
        assert application.sink.page_generation == generation + 1
        assert application.api.get_status()["value"]["pageGeneration"] == generation + 1
        # A later, ordinary page load still gets its own generation.
        application._on_loaded()
        assert application.sink.page_generation == generation + 2

    def test_the_first_heartbeat_counts_as_booted(self, application: Any) -> None:
        window = ReloadableWindow(alive=False)
        application._wire_window(window)
        application._boot_started = 100.0
        application.api.heartbeat()
        application.api.last_heartbeat = 100.0
        assert application._check_renderer(now=130.0) is True


class TestStatusAndStartupFailure:
    """R01/R11: the page reads an authoritative snapshot (get_status) whose
    revision matches the protection events, and a core that fails to build
    is reported instead of every command waiting 25 s for "still starting"."""

    def test_protection_starts_unknown_and_the_event_carries_the_snapshot_revision(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        emitted: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda n, p: emitted.append((n, p))
        )
        monkeypatch.setattr(application, "_hwnd", lambda: 0x1234)
        monkeypatch.setattr(app_module, "CONTENT_PROTECTION_RETRY_S", 0.001)
        assert application.api.get_status()["value"]["protection"] == "unknown"
        application._affinity_api = SlowAffinity(honor_after=99)
        application._apply_content_protection()
        status = application.api.get_status()["value"]
        assert status["protection"] == "unprotected"
        assert emitted[-1][0] == "protection:failed"
        assert emitted[-1][1]["revision"] == status["revision"]
        assert emitted[-1][1]["protection"] == "unprotected"

    def test_a_newer_verdict_has_a_higher_revision_and_a_repeat_keeps_it(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        emitted: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda n, p: emitted.append((n, p))
        )
        monkeypatch.setattr(application, "_hwnd", lambda: 0x1234)
        monkeypatch.setattr(app_module, "CONTENT_PROTECTION_RETRY_S", 0.001)
        application._affinity_api = SlowAffinity(honor_after=99)
        application._apply_content_protection()
        application._affinity_api = SlowAffinity(honor_after=0)
        application._apply_content_protection()
        application._apply_content_protection()  # re-emit after a reload
        revisions = [p["revision"] for _n, p in emitted]
        assert [n for n, _p in emitted] == [
            "protection:failed", "protection:ok", "protection:ok"
        ]
        assert revisions[1] > revisions[0]
        assert revisions[2] == revisions[1]

    def test_a_core_that_fails_to_build_is_reported_and_releases_commands(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as _time

        import app as app_module

        emitted: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda n, p: emitted.append((n, p))
        )
        monkeypatch.setattr(app_module, "crash_log", lambda *a: None)
        monkeypatch.setitem(sys.modules, "httpx", None)  # import fails
        application._build_core()
        assert application.machine is None
        status = application.api.get_status()["value"]
        assert status["core"] == "failed" and status["coreReady"] is False
        assert emitted and emitted[-1][0] == "core:failed"
        assert emitted[-1][1]["revision"] == status["revision"]
        started = _time.monotonic()
        result = application.api.start_session()
        assert _time.monotonic() - started < 5.0, "waited for a core that will never come"
        assert result["ok"] is False
        assert "failed to start" in result["error"]["message"]

    def test_a_built_core_is_announced_ready(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emitted: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            application, "_emit_from_thread", lambda n, p: emitted.append((n, p))
        )
        application._build_core()
        assert emitted[-1][0] == "core:ready"
        assert application.api.core_state == "ready"

    def test_a_loaded_page_gets_a_new_generation_and_a_clean_close_guard(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application._wire_window(StubWindow())
        monkeypatch.setattr(application, "_protect_async", lambda: None)
        application.api.close_guard = True
        before = application.sink.page_generation
        application._on_loaded()
        assert application.sink.page_generation == before + 1
        assert application.api.close_guard is False
        assert application.api.get_status()["value"]["pageGeneration"] == before + 1


class ShutdownMachine:
    """Stands in for SessionManager: cancelling queues the capture stop on the
    audio worker, exactly as `_supersede` -> `_stop_audio` does."""

    def __init__(self, executor: Any, stop: Any) -> None:
        self._active: Any = type("Slot", (), {"id": "s1"})()
        self.executor = executor
        self.stop = stop
        self.cancelled: list[str] = []

    def active_snapshot(self) -> Any:
        return self._active

    def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        self._active = None
        self.executor.submit(self.stop)


class ClosableHttp:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class TestShutdown:
    """The audio worker is a non-daemon thread: without an orderly shutdown
    the interpreter joins it at exit, and a live capture was never stopped."""

    def _run_loop(self, application: Any) -> threading.Thread:
        thread = threading.Thread(target=application.loop.run_forever, daemon=True)
        thread.start()
        return thread

    def test_exit_cancels_the_session_then_drains_and_stops_the_audio_worker(
        self, application: Any
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        order: list[str] = []
        executor = ThreadPoolExecutor(max_workers=1)
        machine = ShutdownMachine(executor, lambda: order.append("capture stopped"))
        application.machine = machine
        application.http = ClosableHttp()
        application._audio_executor = executor
        loop_thread = self._run_loop(application)
        assert application.shutdown(timeout_s=5.0) is True
        loop_thread.join(timeout=5.0)
        assert machine.cancelled == ["s1"]
        assert order == ["capture stopped"], "the queued capture stop never ran"
        assert application.http.closed is True
        with pytest.raises(RuntimeError):
            executor.submit(lambda: None)  # the worker was shut down
        assert not loop_thread.is_alive(), "the core loop was left running"

    def test_a_hung_device_call_reports_an_unclean_exit_within_the_budget(
        self, application: Any
    ) -> None:
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        machine = ShutdownMachine(executor, lambda: release.wait(10.0))
        application.machine = machine
        application._audio_executor = executor
        loop_thread = self._run_loop(application)
        started = _time.monotonic()
        try:
            assert application.shutdown(timeout_s=0.3) is False
            assert _time.monotonic() - started < 3.0
        finally:
            release.set()
            loop_thread.join(timeout=5.0)

    def test_exit_before_the_core_was_built_is_clean(self, application: Any) -> None:
        assert application.shutdown(timeout_s=1.0) is True


class TestCloseGuard:
    """R09: a dirty draft may hold the native close back ONCE so the page can
    ask — but the window must never become permanently uncloseable."""

    def _alive(self, application: Any) -> None:
        booted(application, StubWindow())
        application.api.close_guard = True

    def test_no_guard_closes_immediately(self, application: Any) -> None:
        booted(application, StubWindow())
        assert application._on_closing() is True

    def test_a_guarded_close_is_held_once_then_the_second_attempt_closes(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as _time

        emitted: list[str] = []
        monkeypatch.setattr(application, "_emit_from_thread", lambda n, p: emitted.append(n))
        self._alive(application)
        application.api.last_heartbeat = _time.monotonic()
        assert application._on_closing() is False
        assert emitted == ["window:close-requested"]
        assert application._on_closing() is True, "the window became uncloseable"

    def test_a_page_that_is_not_answering_cannot_hold_the_close(
        self, application: Any
    ) -> None:
        self._alive(application)
        application.api.last_heartbeat = 0.0  # stale: nobody could answer
        assert application._on_closing() is True

    def test_a_page_that_never_booted_cannot_hold_the_close(self, application: Any) -> None:
        import time as _time

        application._wire_window(StubWindow())
        application.api.close_guard = True
        application.api.last_heartbeat = _time.monotonic()
        assert application._on_closing() is True

    def test_the_closing_callback_returns_the_verdict_to_pywebview(
        self, application: Any
    ) -> None:
        import time as _time

        window = StubWindow()
        booted(application, window)
        application.api.close_guard = True
        application.api.last_heartbeat = _time.monotonic()
        handler = window.events.closing.handlers[0]
        assert handler() is False  # pywebview cancels the close on False
        assert handler() is True


class GeometryWindow(StubWindow):
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        super().__init__()
        self.x, self.y, self.width, self.height = x, y, width, height


class TestGeometryPersistence:
    def test_close_flushes_the_pending_debounced_save(self, application: Any) -> None:
        window = GeometryWindow(120, 80, 500, 720)
        application._wire_window(window)
        application._schedule_bounds_save()  # a move 0.5 s ago, timer pending
        assert application._bounds_timer is not None
        application._on_closing()
        # cancel() sets `finished` at once; the thread itself exits a moment
        # later, so is_alive() raced it on a loaded machine.
        assert application._bounds_timer.finished.is_set(), "timer not cancelled"
        assert application.settings.window_bounds() == {
            "x": 120,
            "y": 80,
            "width": 500,
            "height": 720,
        }

    def test_minimized_geometry_is_not_saved_over_real_geometry(
        self, application: Any
    ) -> None:
        application._wire_window(GeometryWindow(120, 80, 500, 720))
        application._save_bounds()
        application._wire_window(GeometryWindow(-32000, -32000, 160, 28))
        application._save_bounds()
        assert application.settings.window_bounds() == {
            "x": 120,
            "y": 80,
            "width": 500,
            "height": 720,
        }

    def test_save_without_a_window_is_harmless(self, application: Any) -> None:
        application._save_bounds()
        assert application.settings.window_bounds() is None


class TestShellHelpers:
    def test_entry_url_prefers_the_dev_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app as app_module

        monkeypatch.setenv("AICA_DEV_URL", "http://localhost:5173")
        assert app_module.resolve_entry_url() == "http://localhost:5173"

    def test_entry_url_falls_back_to_the_built_frontend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        monkeypatch.delenv("AICA_DEV_URL", raising=False)
        url = app_module.resolve_entry_url()
        assert url.endswith(str(Path("frontend") / "dist" / "index.html"))

    def test_loop_exceptions_land_in_the_crash_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        saved = (sys.excepthook, threading.excepthook)
        try:
            app_module.install_crash_logging(tmp_path)
            app_module.loop_exception_handler(
                asyncio.new_event_loop(), {"message": "Task crashed", "exception": ValueError("x")}
            )
        finally:
            faulthandler.disable()
            sys.excepthook, threading.excepthook = saved
        text = (tmp_path / "crash.log").read_text(encoding="utf-8")
        assert "asyncio: Task crashed ValueError('x')" in text

    def test_the_crash_log_rotates_once_it_grows_large(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app as app_module

        monkeypatch.setattr(app_module, "CRASH_LOG_MAX_BYTES", 10)
        (tmp_path / "crash.log").write_text("x" * 50, encoding="utf-8")
        saved = (sys.excepthook, threading.excepthook)
        try:
            app_module.install_crash_logging(tmp_path)
        finally:
            faulthandler.disable()
            sys.excepthook, threading.excepthook = saved
        assert (tmp_path / "crash.log.1").read_text(encoding="utf-8") == "x" * 50
        assert (tmp_path / "crash.log").stat().st_size == 0


class DockableWindow(GeometryWindow):
    """Records pywebview move/resize calls (logical px) and tracks geometry."""

    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        super().__init__(x, y, width, height)
        self.calls: list[tuple[str, int, int]] = []

    def resize(self, width: int, height: int, fix_point: Any = None) -> None:
        self.calls.append(("resize", width, height))
        self.width, self.height = width, height

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))
        self.x, self.y = x, y


class TestInitialPlacement:
    """First run lands top-centre on the primary display, shrunk to fit."""

    def test_no_stored_bounds_docks_top_center_and_fits(self) -> None:
        import app as app_module
        from app_core.store.bounds import WorkArea

        area = WorkArea(0, 0, 1280, 672)
        assert app_module.initial_window_kwargs("full", None, [area]) == {
            "x": 410, "y": 8, "width": 460, "height": 656,
        }
        assert app_module.initial_window_kwargs("prompter", None, [area]) == {
            "x": 280, "y": 8, "width": 720, "height": 260,
        }

    def test_stored_on_screen_bounds_are_restored_verbatim(self) -> None:
        import app as app_module
        from app_core.store.bounds import WorkArea

        stored = {"x": 100, "y": 50, "width": 500, "height": 640}
        assert app_module.initial_window_kwargs(
            "full", stored, [WorkArea(0, 0, 1920, 1040)]
        ) == stored

    def test_off_screen_position_keeps_size_but_goes_top_center(self) -> None:
        import app as app_module
        from app_core.store.bounds import WorkArea

        stored = {"x": 5000, "y": 5000, "width": 500, "height": 600}
        assert app_module.initial_window_kwargs("full", stored, [WorkArea(0, 0, 1920, 1040)]) == {
            "x": 710, "y": 8, "width": 500, "height": 600,
        }

    def test_no_display_information_leaves_placement_to_the_os(self) -> None:
        import app as app_module

        assert app_module.initial_window_kwargs("full", None, []) == {"width": 460, "height": 700}

    def test_primary_is_used_even_when_a_left_monitor_is_listed_first(self) -> None:
        import app as app_module
        from app_core.store.bounds import WorkArea

        kwargs = app_module.initial_window_kwargs(
            "prompter", None, [WorkArea(-1920, 0, 0, 1080), WorkArea(0, 0, 1280, 672)]
        )
        assert kwargs["x"] == 280 and kwargs["y"] == 8

    def test_full_layout_restore_still_clamps_to_the_full_minimum_height(self) -> None:
        import app as app_module
        from app_core.store.bounds import WorkArea

        kwargs = app_module.initial_window_kwargs(
            "full", {"x": 10, "y": 10, "width": 400, "height": 100}, [WorkArea(0, 0, 1920, 1040)]
        )
        assert kwargs["height"] == app_module.FULL_MIN_HEIGHT


class TestDockingAndLayoutSwitch:
    def _dockable(
        self,
        application: Any,
        monkeypatch: pytest.MonkeyPatch,
        areas: list[Any] | None = None,
        current: Any = "laptop",
    ) -> DockableWindow:
        import app as app_module
        from app_core.store.bounds import WorkArea

        laptop = WorkArea(0, 0, 1280, 672)
        all_areas = [laptop] if areas is None else areas
        current_area = laptop if current == "laptop" else current
        window = DockableWindow(120, 80, 500, 720)
        application._wire_window(window)
        monkeypatch.setattr(application, "_runtime_scale", lambda: 1.0)
        monkeypatch.setattr(application, "_window_work_area", lambda: current_area)
        monkeypatch.setattr(app_module, "current_work_areas", lambda divisor=1.0: all_areas)
        return window

    def test_full_geometry_saved_on_another_display_is_restored_exactly(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R12: full view on display A, prompter docked on display B. The old
        code validated A's saved position against B alone, treated it as
        off-screen and docked the full panel on B."""
        from app_core.store.bounds import WorkArea

        display_a = WorkArea(0, 0, 1920, 1040)
        display_b = WorkArea(1920, 0, 3200, 672)
        window = self._dockable(
            application, monkeypatch, areas=[display_a, display_b], current=display_b
        )
        application.settings.set_window_bounds({"x": 120, "y": 80, "width": 500, "height": 720})
        application._layout_mode = "prompter"
        application._apply_settings({"layoutMode": "full", "hotkey": "", "alwaysOnTop": True})
        assert window.calls[-2:] == [("resize", 500, 720), ("move", 120, 80)]

    def test_an_unplugged_display_docks_on_the_current_one(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app_core.store.bounds import WorkArea

        display_b = WorkArea(1920, 0, 3200, 672)
        window = self._dockable(application, monkeypatch, areas=[display_b], current=display_b)
        application.settings.set_window_bounds({"x": 120, "y": 80, "width": 500, "height": 600})
        application._layout_mode = "prompter"
        application._apply_settings({"layoutMode": "full", "hotkey": "", "alwaysOnTop": True})
        # Saved size kept, docked top-centre on the display the window is on.
        assert window.calls[-2:] == [("resize", 500, 600), ("move", 2310, 8)]

    def test_no_display_information_still_applies_the_mode_size(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R12: with no resolvable work area the old code returned early and
        left the prompter strip at the full panel's geometry."""
        window = self._dockable(application, monkeypatch, areas=[], current=None)
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        assert window.calls == [("resize", 720, 260)]

    def test_dock_current_keeps_size_and_moves_to_the_camera_line(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = self._dockable(application, monkeypatch)
        application._dock_current()
        # 720 tall does not fit a 672 work area: shrunk to 656, centred.
        assert window.calls == [("resize", 500, 656), ("move", 390, 8)]

    def test_switching_to_prompter_saves_the_full_geometry_and_docks_the_strip(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = self._dockable(application, monkeypatch)
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        assert application._layout_mode == "prompter"
        assert application.settings.window_bounds() == {
            "x": 120, "y": 80, "width": 500, "height": 720,
        }
        assert window.calls == [("resize", 720, 260), ("move", 280, 8)]
        # A repeated identical view must not move the window again.
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        assert len(window.calls) == 2

    def test_switching_back_restores_the_full_geometry(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = self._dockable(application, monkeypatch)
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        window.calls.clear()
        application._apply_settings({"layoutMode": "full", "hotkey": "", "alwaysOnTop": True})
        assert window.calls == [("resize", 500, 720), ("move", 120, 80)]
        assert application.settings.window_bounds(mode="prompter") == {
            "x": 280, "y": 8, "width": 720, "height": 260,
        }

    def test_a_saved_prompter_position_is_restored_instead_of_docked(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = self._dockable(application, monkeypatch)
        application.settings.set_window_bounds(
            {"x": 40, "y": 300, "width": 800, "height": 200}, mode="prompter"
        )
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        assert window.calls == [("resize", 800, 200), ("move", 40, 300)]

    def test_an_off_screen_prompter_position_keeps_its_size_but_docks(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = self._dockable(application, monkeypatch)
        application.settings.set_window_bounds(
            {"x": 5000, "y": 5000, "width": 800, "height": 200}, mode="prompter"
        )
        application._apply_settings({"layoutMode": "prompter", "hotkey": "", "alwaysOnTop": True})
        assert window.calls == [("resize", 800, 200), ("move", 240, 8)]

    def test_bounds_saves_land_under_the_current_mode_key(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._dockable(application, monkeypatch)
        application._layout_mode = "prompter"
        application._save_bounds()
        assert application.settings.window_bounds() is None
        assert application.settings.window_bounds(mode="prompter") == {
            "x": 120, "y": 80, "width": 500, "height": 720,
        }

    def test_runtime_work_areas_are_divided_by_the_scale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import win32api

        import app as app_module

        monkeypatch.setattr(win32api, "EnumDisplayMonitors", lambda: [(1, None, None)])
        monkeypatch.setattr(win32api, "GetMonitorInfo", lambda h: {"Work": (0, 0, 1920, 1008)})
        from app_core.store.bounds import WorkArea

        assert app_module.current_work_areas(divisor=1.5) == [WorkArea(0, 0, 1280, 672)]


class TestDeferredCore:
    def test_the_window_can_exist_before_the_core_is_built(self, application: Any) -> None:
        # Nothing heavy has been imported or built by construction time.
        assert application.machine is None and application.http is None
        assert not application.api._core_ready.is_set()

    def test_build_core_hands_the_machine_to_the_bridge(self, application: Any) -> None:
        application._build_core()
        assert application.machine is not None
        assert application.api._core_ready.is_set()
        assert application.api._machine is application.machine

    def test_shown_and_loaded_restart_the_watchdog_clock(
        self, application: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow cold start (WebView2 boot, first page load) must not be
        mistaken for a dead renderer and reloaded mid-boot."""
        window = ReloadableWindow(alive=True)
        application._wire_window(window)
        application.api.last_heartbeat = 0.0
        monkeypatch.setattr(application, "_protect_async", lambda: None)
        application._on_shown()
        assert application.api.last_heartbeat > 0.0
        application.api.last_heartbeat = 0.0
        application._on_loaded()
        assert application.api.last_heartbeat > 0.0
