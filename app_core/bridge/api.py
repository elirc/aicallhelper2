"""The pywebview js_api object — every command the frontend can call.

Every command returns a Result envelope `{ok: true, value} | {ok: false,
error: {code, message}}` rather than throwing across the boundary
(validation errors included). pywebview invokes these methods on worker
threads: each handler hands off to the core's asyncio loop and blocks its
worker thread on the result — session state is only ever touched on the
loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import webbrowser
from collections.abc import Callable, Coroutine
from typing import Any

from app_core.errors import AppError
from app_core.session.machine import SessionManager
from app_core.store.settings import SettingsStore

Envelope = dict[str, Any]
COMMAND_TIMEOUT_S = 30.0


def _ok(value: object) -> Envelope:
    return {"ok": True, "value": value}


def _err(err: AppError) -> Envelope:
    return {"ok": False, "error": err.to_payload()}


class JsApi:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        machine: SessionManager,
        settings: SettingsStore,
        *,
        hotkey_registered: Callable[[], bool] = lambda: False,
        on_settings_changed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._loop = loop
        self._machine = machine
        self._settings = settings
        self._hotkey_registered = hotkey_registered
        self._on_settings_changed = on_settings_changed
        self.last_heartbeat = time.monotonic()

    # ------------------------------------------------------------- commands

    def get_settings(self) -> Envelope:
        return self._call(self._get_settings())

    def set_settings(self, patch: object) -> Envelope:
        if not isinstance(patch, dict):
            return _err(AppError("internal", "Settings patch must be an object."))
        return self._call(self._set_settings(patch))

    def start_session(self) -> Envelope:
        return self._call(self._machine.start_session())

    def stop_session(self, session_id: object) -> Envelope:
        if not isinstance(session_id, str):
            return _err(AppError("internal", "Stop not taken — bad session id."))
        return self._call(self._machine.stop_session(session_id))

    def ask(self, text: object) -> Envelope:
        if not isinstance(text, str):
            return _err(AppError("internal", "Type a question first."))
        return self._call(self._machine.ask(text))

    def cancel_session(self, session_id: object) -> Envelope:
        # Fire-and-forget; invalid ids do nothing — never an error.
        if isinstance(session_id, str):
            self._loop.call_soon_threadsafe(self._machine.cancel_session, session_id)
        return _ok(None)

    def heartbeat(self) -> Envelope:
        # Renderer liveness ping (crash-recovery fallback).
        self.last_heartbeat = time.monotonic()
        return _ok(None)

    def open_external(self, url: object) -> Envelope:
        # https only; the app never navigates the webview away from its page.
        if isinstance(url, str) and url.startswith("https://"):
            webbrowser.open(url)
            return _ok(None)
        return _err(AppError("internal", "Only https links can be opened."))

    # ------------------------------------------------------------ internals

    def _call(self, coro: Coroutine[object, object, object]) -> Envelope:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return _ok(future.result(timeout=COMMAND_TIMEOUT_S))
        except AppError as err:
            return _err(err)
        except Exception:
            future.cancel()
            return _err(AppError("internal", "Something went wrong inside the app core."))

    def _decorate(self, view: dict[str, Any]) -> dict[str, Any]:
        return {**view, "hotkeyRegistered": self._hotkey_registered()}

    async def _get_settings(self) -> dict[str, Any]:
        view = await asyncio.to_thread(self._settings.view)
        return self._decorate(view)

    async def _set_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        view = await asyncio.to_thread(self._settings.patch, patch)
        if self._on_settings_changed is not None:
            # Window/hotkey application must not fail the save.
            with contextlib.suppress(Exception):
                self._on_settings_changed(view)
        return self._decorate(view)
