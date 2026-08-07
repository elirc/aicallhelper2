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
from app_core.bridge.api import JsApi
from app_core.bridge.events import WebviewEventSink
from app_core.errors import AppError


class FakeMachine:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

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


def decode_detail(code: str) -> dict[str, Any]:
    prefix = "JSON.parse("
    start = code.index(prefix) + len(prefix)
    literal = code[start:-4]  # strip the trailing )}))
    payload = json.loads(json.loads(literal))
    assert isinstance(payload, dict)
    return payload


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
        while len(window.scripts) < 5 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        details = [decode_detail(code) for code in window.scripts]
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
        assert decode_detail(window.scripts[0])["payload"]["text"] == "early"

    async def test_a_throwing_webview_does_not_kill_the_pump(self) -> None:
        sink = WebviewEventSink()
        window = FakeWindow(fail_first=True)
        sink.attach(window)
        sink.start()
        sink.emit("a", {"n": 1})
        sink.emit("b", {"n": 2})
        deadline = asyncio.get_running_loop().time() + 2.0
        while not window.scripts and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert decode_detail(window.scripts[0])["name"] == "b"
