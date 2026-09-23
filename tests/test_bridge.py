"""The pywebview bridge: Result envelopes, thread handoff, event ordering.

JsApi is exercised the way pywebview drives it — synchronous calls from a
foreign thread against a core loop running elsewhere. The event sink is
exercised against a fake window capturing evaluate_js scripts.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import app_core.bridge.api as api_module
import app_core.bridge.events as events_module
from app_core.bridge.api import JsApi
from app_core.bridge.events import WebviewEventSink
from app_core.errors import AppError


class FakeMachine:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def active_snapshot(self) -> Any:
        return None  # idle unless a subclass installs a slot

    async def start_session(self) -> str:
        return "s1"

    async def stop_session(self, session_id: str) -> None:
        raise AppError("internal", "Stop not taken — that session is not recording.")

    async def ask(self, text: str) -> str:
        if text == "boom":
            raise RuntimeError("secret internal detail")
        return "s2"

    def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class FakeStore:
    def __init__(self) -> None:
        self.patches: list[dict[str, Any]] = []

    def view(self) -> dict[str, Any]:
        return {"resume": "", "hotkey": "Ctrl+R"}

    def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.patches.append(patch)
        if patch.get("bad"):
            raise AppError("internal", "Bad patch.")
        return self.view()


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    new_loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(new_loop)
        new_loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield new_loop
    new_loop.call_soon_threadsafe(new_loop.stop)
    thread.join(timeout=2.0)


def make_api(
    loop: asyncio.AbstractEventLoop,
    machine: FakeMachine | None = None,
    store: FakeStore | None = None,
    **kwargs: Any,
) -> tuple[JsApi, FakeMachine, FakeStore]:
    machine = machine or FakeMachine()
    store = store or FakeStore()
    api = JsApi(loop, machine, store, **kwargs)  # type: ignore[arg-type]
    return api, machine, store


class TestEnvelopes:
    def test_ok_envelope(self, loop: asyncio.AbstractEventLoop) -> None:
        api, _machine, _store = make_api(loop)
        assert api.start_session() == {"ok": True, "value": "s1"}

    def test_app_error_becomes_error_envelope(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        result = api.stop_session("s1")
        assert result["ok"] is False
        assert result["error"]["code"] == "internal"
        assert "not taken" in result["error"]["message"].lower()

    def test_unexpected_exception_never_leaks_its_text(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        result = api.ask("boom")
        assert result["ok"] is False
        assert result["error"]["code"] == "internal"
        assert "secret internal detail" not in result["error"]["message"]

    def test_type_validation_rejected_without_reaching_the_loop(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        assert api.ask(42)["ok"] is False
        assert api.stop_session(None)["ok"] is False
        assert api.set_settings("not a dict")["ok"] is False


class TestCommands:
    def test_cancel_is_fire_and_forget_and_reaches_the_machine(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, machine, _store = make_api(loop)
        assert api.cancel_session("s9") == {"ok": True, "value": None}
        deadline = time.monotonic() + 2.0
        while not machine.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert machine.cancelled == ["s9"]

    def test_cancel_with_bad_id_type_is_still_ok(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, machine, _store = make_api(loop)
        assert api.cancel_session(123)["ok"] is True
        assert machine.cancelled == []

    def test_get_settings_merges_hotkey_registration_state(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop, hotkey_registered=lambda: True)
        result = api.get_settings()
        assert result["ok"] is True
        assert result["value"]["hotkeyRegistered"] is True

    def test_set_settings_notifies_and_a_failing_hook_never_fails_the_save(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        seen: list[dict[str, Any]] = []

        def hook(view: dict[str, Any]) -> None:
            seen.append(view)
            raise RuntimeError("hotkey application exploded")

        api, _machine, store = make_api(loop, on_settings_changed=hook)
        result = api.set_settings({"resume": "hi"})
        assert result["ok"] is True
        assert seen and store.patches == [{"resume": "hi"}]

    def test_the_settings_hook_runs_off_the_core_loop(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Applying settings re-registers the global hotkey, which joins the
        old message-loop thread and waits on the new one — blocking work
        that must never stall the loop that pumps audio frames and events."""
        loop_thread: list[int] = []
        loop.call_soon_threadsafe(lambda: loop_thread.append(threading.get_ident()))
        hook_thread: list[int] = []

        def hook(view: dict[str, Any]) -> None:
            hook_thread.append(threading.get_ident())

        api, _machine, _store = make_api(loop, on_settings_changed=hook)
        assert api.set_settings({"hotkey": "Ctrl+R"})["ok"] is True
        assert hook_thread and loop_thread
        assert hook_thread[0] != loop_thread[0], "the hook ran on the core loop"

    def test_heartbeat_updates_liveness(self, loop: asyncio.AbstractEventLoop) -> None:
        api, _machine, _store = make_api(loop)
        api.last_heartbeat = 0.0
        assert api.heartbeat()["ok"] is True
        assert api.last_heartbeat > 0.0

    def test_open_external_https_only(
        self, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(
            api_module.webbrowser, "open", lambda url: opened.append(url)
        )
        api, _machine, _store = make_api(loop)
        assert api.open_external("https://example.com")["ok"] is True
        assert api.open_external("http://example.com")["ok"] is False
        assert api.open_external("javascript:alert(1)")["ok"] is False
        assert api.open_external(5)["ok"] is False
        assert opened == ["https://example.com"]


class FakeWindow:
    def __init__(self, fail_first: bool = False) -> None:
        self.scripts: list[str] = []
        self._fail_first = fail_first

    def evaluate_js(self, code: str) -> None:
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("renderer is gone")
        self.scripts.append(code)


def decode_details(code: str) -> list[dict[str, Any]]:
    """One evaluate_js call carries a BATCH of events (see events.MAX_BATCH)."""
    prefix = "JSON.parse("
    start = code.index(prefix) + len(prefix)
    end = code.index(").forEach(")
    payload = json.loads(json.loads(code[start:end]))
    assert isinstance(payload, list)
    return payload


def all_details(scripts: list[str]) -> list[dict[str, Any]]:
    return [detail for code in scripts for detail in decode_details(code)]


class TestEventSink:
    async def test_events_arrive_in_emission_order_with_payloads_intact(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        hostile = 'quote " backslash \\ newline \n unicode é'
        for i in range(5):
            sink.emit("llm:delta", {"sessionId": "s1", "delta": f"{hostile}{i}"})
        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            len(all_details(window.scripts)) < 5
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        details = all_details(window.scripts)
        assert [d["name"] for d in details] == ["llm:delta"] * 5
        assert [d["payload"]["delta"] for d in details] == [
            f"{hostile}{i}" for i in range(5)
        ]

    async def test_events_before_attach_are_held_not_dropped(self) -> None:
        sink = WebviewEventSink()
        sink.start()
        sink.emit("stt:partial", {"sessionId": "s1", "text": "early"})
        await asyncio.sleep(0.15)
        window = FakeWindow()
        sink.attach(window)
        deadline = asyncio.get_running_loop().time() + 2.0
        while not window.scripts and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert all_details(window.scripts)[0]["payload"]["text"] == "early"

    async def test_a_transient_dispatch_failure_loses_nothing(self) -> None:
        # A failed batch is retried event by event, so a momentary webview
        # hiccup no longer costs the whole batch — losing a terminal
        # llm:done would strand the UI in "Generating answer..." forever.
        sink = WebviewEventSink()
        window = FakeWindow(fail_first=True)
        sink.attach(window)
        sink.start()
        sink.emit("a", {"n": 1})
        sink.emit("b", {"n": 2})
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            len(all_details(window.scripts)) < 2
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        assert [d["name"] for d in all_details(window.scripts)] == ["a", "b"]

    async def test_one_poison_event_cannot_take_its_neighbours_down(self) -> None:
        # An unserializable payload used to kill the pump task outright
        # (json.dumps ran outside the guard); now it costs only itself.
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "before"})
        sink.emit("bad", {"sessionId": "s1", "obj": object()})  # not JSON
        sink.emit("llm:done", {"sessionId": "s1", "answer": "after"})
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            len(all_details(window.scripts)) < 2
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        names = [d["name"] for d in all_details(window.scripts)]
        assert names == ["llm:delta", "llm:done"]  # the terminal event survived

    async def test_non_finite_numbers_do_not_produce_invalid_json(self) -> None:
        # JSON.parse rejects NaN/Infinity, so a batch containing one would be
        # dropped wholesale by the page.
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        sink.emit("audio:level", {"sessionId": "s1", "rms": float("nan")})
        sink.emit("audio:level", {"sessionId": "s1", "rms": 0.5})
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            not all_details(window.scripts)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        details = all_details(window.scripts)
        assert [d["payload"]["rms"] for d in details] == [0.5]
        for code in window.scripts:
            assert "NaN" not in code


class TestEventBatching:
    """Each evaluate_js is a blocking round trip on a worker thread and an
    answer streams dozens of deltas per second, so the pump batches whatever
    is already queued — without ever reordering."""

    async def test_a_burst_is_delivered_in_one_call_in_order(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        for i in range(20):
            sink.emit("llm:delta", {"sessionId": "s1", "delta": str(i)})
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            len(all_details(window.scripts)) < 20
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        details = all_details(window.scripts)
        assert [d["payload"]["delta"] for d in details] == [str(i) for i in range(20)]
        # The whole burst was already queued, so it must not cost 20 hops.
        assert len(window.scripts) < 20

    async def test_batches_never_exceed_the_cap(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        total = events_module.MAX_BATCH * 2 + 5
        for i in range(total):
            sink.emit("llm:delta", {"sessionId": "s1", "delta": str(i)})
        deadline = asyncio.get_running_loop().time() + 5.0
        while (
            len(all_details(window.scripts)) < total
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        assert all(
            len(decode_details(code)) <= events_module.MAX_BATCH for code in window.scripts
        )
        assert [d["payload"]["delta"] for d in all_details(window.scripts)] == [
            str(i) for i in range(total)
        ]

    async def test_mixed_event_names_keep_their_relative_order(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        emitted = [
            ("stt:partial", {"sessionId": "s1", "text": "q"}),
            ("llm:delta", {"sessionId": "s1", "delta": "a"}),
            ("llm:delta", {"sessionId": "s1", "delta": "b"}),
            ("llm:done", {"sessionId": "s1", "answer": "ab"}),
        ]
        for name, payload in emitted:
            sink.emit(name, payload)
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            len(all_details(window.scripts)) < len(emitted)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        assert [d["name"] for d in all_details(window.scripts)] == [n for n, _ in emitted]

    async def test_javascript_line_separators_cannot_break_out_of_the_script(self) -> None:
        # U+2028/U+2029 are line terminators in JS source; the outer dump must
        # escape them or the generated script is syntactically broken.
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        hostile = "line" + chr(0x2028) + "sep" + chr(0x2029) + r"""para</script>\ " '"""
        sink.emit("llm:delta", {"sessionId": "s1", "delta": hostile})
        deadline = asyncio.get_running_loop().time() + 2.0
        while not window.scripts and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert all_details(window.scripts)[0]["payload"]["delta"] == hostile
        # Escaped as  , never emitted raw into the JS source.
        assert chr(0x2028) not in window.scripts[0]
        assert chr(0x2029) not in window.scripts[0]


class GatedWindow:
    """evaluate_js blocks while `gate` is closed (a hung renderer), then runs
    the script — i.e. the abandoned call still executes LATE."""

    def __init__(self, hang_first: int) -> None:
        self.scripts: list[str] = []
        self.calls = 0
        self.hang_first = hang_first
        self.gate = threading.Event()
        self._lock = threading.Lock()

    def evaluate_js(self, code: str) -> None:
        with self._lock:
            self.calls += 1
            hang = self.calls <= self.hang_first
        if hang:
            self.gate.wait(10.0)
        with self._lock:
            self.scripts.append(code)


async def wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


class TestBoundedDelivery:
    """R07: a timed-out dispatch is an uncertain delivery, the queue must be
    bounded without ever losing terminal events, and audio levels coalesce
    without losing their session."""

    async def test_every_payload_carries_seq_and_page_generation(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow()
        sink.attach(window)
        sink.start()
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "a"})
        await wait_until(lambda: len(all_details(window.scripts)) == 1)
        sink.new_page()
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "b"})
        await wait_until(lambda: len(all_details(window.scripts)) == 2)
        first, second = (d["payload"] for d in all_details(window.scripts))
        assert second["seq"] > first["seq"]
        assert second["pageGen"] == first["pageGen"] + 1

    def test_audio_levels_coalesce_per_session_keeping_identity(self) -> None:
        sink = WebviewEventSink()  # not started: everything stays pending
        sink.emit("audio:level", {"sessionId": "s1", "rms": 0.1})
        sink.emit("stt:partial", {"sessionId": "s1", "text": "hi", "isFinal": False})
        sink.emit("audio:level", {"sessionId": "s2", "rms": 0.2})
        sink.emit("audio:level", {"sessionId": "s1", "rms": 0.3})
        sink.emit("audio:level", {"sessionId": "s2", "rms": 0.4})
        pending = sink.pending()
        levels = [p for n, p in pending if n == "audio:level"]
        assert [(p["sessionId"], p["rms"]) for p in levels] == [("s1", 0.3), ("s2", 0.4)]
        # The coalesced entry keeps its original place and seq, so ordering
        # by seq on the page still matches queue order.
        seqs = [p["seq"] for _n, p in pending]
        assert seqs == sorted(seqs)

    def test_the_bound_sheds_expendables_and_never_terminal_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(events_module, "MAX_PENDING", 10)
        sink = WebviewEventSink()
        for i in range(30):
            sink.emit("llm:delta", {"sessionId": "s1", "delta": str(i)})
            sink.emit("audio:level", {"sessionId": f"s{i}", "rms": 0.5})
            if i % 10 == 0:
                sink.emit("session:error", {"sessionId": f"e{i}", "error": {}})
        sink.emit("llm:done", {"sessionId": "s1", "answer": "full"})
        sink.emit("protection:failed", {"revision": 4})
        pending = sink.pending()
        names = [n for n, _p in pending]
        assert len(pending) <= 10 + 5  # bound, plus reserved events
        assert names.count("session:error") == 3
        assert "llm:done" in names and "protection:failed" in names
        assert "audio:level" not in names, "levels are shed before deltas"
        assert sink.shed > 0
        seqs = [p["seq"] for _n, p in pending]
        assert seqs == sorted(seqs), "shedding reordered the queue"

    async def test_a_timed_out_batch_is_not_duplicated_but_its_terminal_event_arrives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old retry re-sent every event of a timed-out batch one by one;
        when the first call executed late anyway, every delta showed twice."""
        monkeypatch.setattr(events_module, "DISPATCH_TIMEOUT_S", 0.1)
        monkeypatch.setattr(events_module, "RENDERER_BACKOFF_S", 0.01)
        sink = WebviewEventSink()
        window = GatedWindow(hang_first=1)
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "A"})
        sink.emit("llm:delta", {"sessionId": "s1", "delta": "B"})
        sink.emit("llm:done", {"sessionId": "s1", "answer": "AB"})
        sink.attach(window)
        sink.start()
        # The terminal event is re-sent after the timeout and gets through.
        await wait_until(lambda: any(d["name"] == "llm:done" for d in all_details(window.scripts)))
        window.gate.set()  # the abandoned first call now executes, late
        await wait_until(lambda: len(window.scripts) >= 2)
        await asyncio.sleep(0.2)
        details = all_details(window.scripts)
        deltas = [d["payload"]["delta"] for d in details if d["name"] == "llm:delta"]
        assert deltas == ["A", "B"], "a delta was delivered twice (or lost from the late call)"
        dones = [d["payload"]["seq"] for d in details if d["name"] == "llm:done"]
        assert len(set(dones)) == 1, "a retry must carry the same seq for page dedup"

    async def test_a_permanently_hung_renderer_does_not_pile_up_threads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(events_module, "DISPATCH_TIMEOUT_S", 0.02)
        monkeypatch.setattr(events_module, "RENDERER_BACKOFF_S", 0.01)
        monkeypatch.setattr(events_module, "MAX_HUNG_DISPATCHES", 2)
        sink = WebviewEventSink()
        window = GatedWindow(hang_first=10_000)
        sink.attach(window)
        sink.start()
        sink.emit("llm:done", {"sessionId": "s1", "answer": "x"})  # retried forever
        await asyncio.sleep(0.5)

        def held() -> bool:
            return [n for n, _p in sink.pending()] == ["llm:done"]

        # Poll rather than sample once: under a loaded suite the single sample
        # could land while the event is taken for a (refused) dispatch attempt.
        await wait_until(held)
        try:
            assert window.calls <= 2, f"{window.calls} threads stuck in the renderer"
            assert held(), "terminal event lost"
        finally:
            window.gate.set()

    async def test_a_reloaded_page_gets_deliveries_again_after_the_old_one_hung(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A call against a crashed renderer may never return, so the hung
        # count must not outlive the page it hung on: after a reload the
        # healthy page gets events again instead of a pump that ratcheted shut.
        monkeypatch.setattr(events_module, "DISPATCH_TIMEOUT_S", 0.02)
        monkeypatch.setattr(events_module, "RENDERER_BACKOFF_S", 0.01)
        monkeypatch.setattr(events_module, "MAX_HUNG_DISPATCHES", 2)
        sink = WebviewEventSink()
        window = GatedWindow(hang_first=2)  # the old page swallows two calls, forever
        sink.attach(window)
        sink.start()
        sink.emit("llm:done", {"sessionId": "s1", "answer": "x"})
        try:
            await wait_until(lambda: window.calls >= 2)
            await asyncio.sleep(0.2)
            assert window.calls == 2, "the pump kept spawning threads at a dead page"
            sink.new_page()  # the watchdog reloaded; calls 3+ answer normally
            await wait_until(lambda: len(all_details(window.scripts)) >= 1)
            assert [d["name"] for d in all_details(window.scripts)] == ["llm:done"]
        finally:
            window.gate.set()


class TestDockAndCoreReadiness:
    def test_dock_window_calls_the_shell_hook_and_returns_ok(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        docked: list[bool] = []
        api, _machine, _store = make_api(loop, on_dock=lambda: docked.append(True))
        assert api.dock_window() == {"ok": True, "value": None}
        assert docked == [True]

    def test_dock_window_without_a_hook_is_a_no_op(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        assert api.dock_window() == {"ok": True, "value": None}

    def test_a_raising_dock_hook_still_returns_ok(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        def boom() -> None:
            raise RuntimeError("no window")

        api, _machine, _store = make_api(loop, on_dock=boom)
        assert api.dock_window() == {"ok": True, "value": None}

    def test_session_commands_wait_for_a_late_core_then_run(
        self, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shell hands the machine over AFTER the window exists; a start
        pressed in that gap waits (bounded) instead of failing."""
        monkeypatch.setattr(api_module, "CORE_READY_TIMEOUT_S", 2.0)
        api = JsApi(loop, None, FakeStore())  # type: ignore[arg-type]
        machine = FakeMachine()
        threading.Timer(0.1, lambda: api._attach_core(machine)).start()  # type: ignore[arg-type]
        assert api.start_session() == {"ok": True, "value": "s1"}

    def test_session_commands_fail_actionably_if_the_core_never_arrives(
        self, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_module, "CORE_READY_TIMEOUT_S", 0.05)
        api = JsApi(loop, None, FakeStore())  # type: ignore[arg-type]
        result = api.start_session()
        assert result["ok"] is False
        assert "starting up" in result["error"]["message"]
        # Settings never need the core.
        assert api.get_settings()["ok"] is True
        assert api.cancel_session("s1") == {"ok": True, "value": None}

    def test_a_failed_core_releases_a_waiting_command_at_once(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """R11: a core build that raised used to leave every command waiting
        the full 25 s and then claiming the app was "still starting"."""
        api = JsApi(loop, None, FakeStore())  # type: ignore[arg-type]
        results: list[Any] = []
        waiter = threading.Thread(target=lambda: results.append(api.start_session()))
        waiter.start()
        time.sleep(0.1)
        api._core_failed("ImportError")
        waiter.join(timeout=5.0)
        assert not waiter.is_alive(), "the command kept waiting for a dead core"
        assert results[0]["ok"] is False
        assert "failed to start" in results[0]["error"]["message"]
        assert "starting up" not in results[0]["error"]["message"]


# ------------------------------------------------------------ status (R01/R11)


class SlotMachine(FakeMachine):
    """FakeMachine with a settable active slot, read through the same public
    accessor as SessionManager.active_snapshot (aborted slots read as idle)."""

    def __init__(self) -> None:
        super().__init__()
        self._active: Any = None

    def active_snapshot(self) -> Any:
        slot = self._active
        return None if slot is None or slot.aborted else slot


class FakeSlot:
    def __init__(self, session_id: str, phase: str, aborted: bool = False) -> None:
        self.id, self.phase, self.aborted = session_id, phase, aborted


class TestStatusSnapshot:
    def test_shape_before_the_core_exists(self, loop: asyncio.AbstractEventLoop) -> None:
        api = JsApi(loop, None, FakeStore(), page_generation=lambda: 3)  # type: ignore[arg-type]
        result = api.get_status()
        assert result["ok"] is True
        status = result["value"]
        assert status["coreReady"] is False and status["core"] == "starting"
        assert status["protection"] == "unknown"
        assert status["session"] == {"id": None, "phase": "idle"}
        assert status["pageGeneration"] == 3
        assert isinstance(status["revision"], int)
        json.dumps(status)  # JSON-able across the bridge

    def test_the_revision_moves_only_when_a_field_changes(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        first = api.get_status()["value"]["revision"]
        assert api.get_status()["value"]["revision"] == first
        protected_rev = api._set_protection(True)
        assert protected_rev > first
        assert api._set_protection(True) == protected_rev, "a repeat verdict bumped"
        status = api.get_status()["value"]
        assert status["revision"] == protected_rev and status["protection"] == "protected"
        failed_rev = api._set_protection(False)
        assert failed_rev > protected_rev
        assert api.get_status()["value"]["protection"] == "unprotected"

    def test_the_page_cannot_set_the_protection_verdict(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        # pywebview exposes every public js_api method to page JS; the verdict
        # must only ever come from the Windows read-back, never from the page.
        api, _machine, _store = make_api(loop)
        exposed = {name for name in dir(api) if not name.startswith("_")}
        assert not any("protection" in name and name.startswith("set") for name in exposed)

    def test_the_live_session_is_reported_in_page_phases(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        machine = SlotMachine()
        api, _machine, _store = make_api(loop, machine=machine)
        idle = api.get_status()["value"]
        assert idle["session"] == {"id": None, "phase": "idle"}
        for machine_phase, page_phase in (
            ("connecting", "recording"),
            ("recording", "recording"),
            ("finalizing", "finalizing"),
            ("answering", "answering"),
        ):
            machine._active = FakeSlot("s7", machine_phase)
            status = api.get_status()["value"]
            assert status["session"] == {"id": "s7", "phase": page_phase}
        assert status["revision"] > idle["revision"]
        machine._active = FakeSlot("s7", "recording", aborted=True)
        assert api.get_status()["value"]["session"]["phase"] == "idle"
        machine._active = FakeSlot("s7", "done")
        assert api.get_status()["value"]["session"]["phase"] == "idle"

    def test_a_stalled_loop_reports_unknown_instead_of_hanging(
        self, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_module, "STATUS_SESSION_TIMEOUT_S", 0.05)
        api, _machine, _store = make_api(loop)
        release = threading.Event()
        loop.call_soon_threadsafe(lambda: release.wait(2.0))  # wedge the loop
        try:
            status = api.get_status()["value"]
        finally:
            release.set()
        assert status["session"]["phase"] == "unknown"

    def test_core_failure_is_in_the_snapshot(self, loop: asyncio.AbstractEventLoop) -> None:
        api = JsApi(loop, None, FakeStore())  # type: ignore[arg-type]
        before = api.get_status()["value"]["revision"]
        revision = api._core_failed("ImportError")
        status = api.get_status()["value"]
        assert revision > before and status["revision"] == revision
        assert status["core"] == "failed" and status["coreError"] == "ImportError"


class SlowStartMachine(FakeMachine):
    """start_session completes AFTER the bridge's deadline, and cannot be
    interrupted (it blocks the loop), like a start whose DPAPI reads stall."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def start_session(self) -> str:
        time.sleep(self.delay_s)
        return "s-late"

    async def ask(self, text: str) -> str:
        time.sleep(self.delay_s)
        return "s-late-ask"


class TestLateCommandResults:
    """A start that completes just after the 30 s bridge timeout used to leave
    a session recording while the page was told the start failed —
    `future.cancel()` cannot stop a coroutine that already finished."""

    @pytest.mark.parametrize("command", ["start_session", "ask"])
    def test_a_late_session_is_cancelled_and_only_that_one(
        self,
        loop: asyncio.AbstractEventLoop,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
    ) -> None:
        monkeypatch.setattr(api_module, "COMMAND_TIMEOUT_S", 0.05)
        machine = SlowStartMachine(delay_s=0.3)
        api, _machine, _store = make_api(loop, machine=machine)
        call = getattr(api, command)
        result = call() if command == "start_session" else call("question")
        assert result["ok"] is False
        deadline = time.monotonic() + 3.0
        while not machine.cancelled and time.monotonic() < deadline:
            time.sleep(0.02)
        expected = "s-late" if command == "start_session" else "s-late-ask"
        assert machine.cancelled == [expected]

    def test_an_on_time_start_is_never_reaped(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, machine, _store = make_api(loop)
        assert api.start_session() == {"ok": True, "value": "s1"}
        time.sleep(0.1)
        assert machine.cancelled == []


class TestShellCommands:
    def test_open_external_rejects_urls_the_shell_could_misread(
        self, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(api_module.webbrowser, "open", lambda url: opened.append(url))
        api, _machine, _store = make_api(loop)
        for bad in (
            "https://",  # no host
            "https:///path",
            "https://example.com/a b",  # whitespace
            "https://example.com/\x00",
            "https://example.com/\n--flag",
            "https://" + "a" * 3000 + ".com",
        ):
            assert api.open_external(bad)["ok"] is False, repr(bad)
        assert opened == []
        assert api.open_external("https://console.anthropic.com/settings/keys")["ok"] is True

    def test_close_guard_accepts_only_a_real_true(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        api, _machine, _store = make_api(loop)
        assert api.set_close_guard(True) == {"ok": True, "value": None}
        assert api.close_guard is True
        for other in (False, "true", 1, None):
            api.set_close_guard(other)
            assert api.close_guard is False

    def test_a_heartbeat_marks_the_page_booted(self, loop: asyncio.AbstractEventLoop) -> None:
        api, _machine, _store = make_api(loop)
        assert api.heartbeat_seen is False
        api.heartbeat()
        assert api.heartbeat_seen is True


class OrderedStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.log: list[str] = []

    def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.log.append(f"patch {patch['n']}")
        return super().patch(patch)


class TestSaveSerialization:
    def test_a_save_and_its_side_effects_finish_before_the_next_save_starts(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """R09: two saves in flight used to interleave patch/hook pairs, so a
        slow layout switch from save 1 could land after save 2's."""
        store = OrderedStore()
        first_hook_entered = threading.Event()

        def hook(view: dict[str, Any]) -> None:
            n = len([e for e in store.log if e.startswith("patch")])
            if n == 1:
                first_hook_entered.set()
                time.sleep(0.2)  # a slow window/hotkey application
            store.log.append(f"hook {n}")

        api, _machine, _store = make_api(loop, store=store, on_settings_changed=hook)
        first = threading.Thread(target=lambda: api.set_settings({"n": 1}))
        first.start()
        assert first_hook_entered.wait(3.0)
        assert api.set_settings({"n": 2})["ok"] is True
        first.join(timeout=5.0)
        assert store.log == ["patch 1", "hook 1", "patch 2", "hook 2"]
