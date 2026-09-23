"""The pywebview js_api object — every command the frontend can call.

Every command returns a Result envelope `{ok: true, value} | {ok: false,
error: {code, message}}` rather than throwing across the boundary
(validation errors included). pywebview invokes these methods on worker
threads: each handler hands off to the core's asyncio loop and blocks its
worker thread on the result — session state is only ever touched on the
loop.

The session machine may arrive AFTER the window (the shell builds the heavy
core on a background thread so the window appears first — see app.py
`App._build_core`). Session commands wait on `_core_ready` for a bounded
time; settings commands never need the machine and are served immediately.
If the build FAILS, `_core_failed` releases every waiter at once with the
startup error instead of letting each command time out as "still starting".

`get_status()` is the page's authoritative snapshot (core readiness, the
capture-protection verdict, the live session). It carries a monotonic
`revision`; protection push events carry the same counter, so a page that
reads a snapshot and also receives events can always tell which is newer.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import webbrowser
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from app_core.errors import AppError

if TYPE_CHECKING:
    from app_core.session.machine import SessionManager
    from app_core.store.settings import SettingsStore

Envelope = dict[str, Any]
COMMAND_TIMEOUT_S = 30.0
CORE_READY_TIMEOUT_S = 25.0
# get_status must answer even while the loop is busy: a stalled loop reports
# the session as "unknown" rather than hanging the page's mount.
STATUS_SESSION_TIMEOUT_S = 2.0

Protection = Literal["protected", "unprotected", "unknown"]
CoreState = Literal["starting", "ready", "failed"]

# Machine phase -> the phase vocabulary the page understands. "connecting"
# is already capturing audio and accepts Stop, so to the user it IS
# recording; "done" is a session on its way out of the slot.
_PAGE_PHASES = {
    "connecting": "recording",
    "recording": "recording",
    "finalizing": "finalizing",
    "answering": "answering",
}
_IDLE_SESSION: dict[str, object] = {"id": None, "phase": "idle"}


def _ok(value: object) -> Envelope:
    return {"ok": True, "value": value}


def _err(err: AppError) -> Envelope:
    return {"ok": False, "error": err.to_payload()}


class JsApi:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        machine: SessionManager | None,
        settings: SettingsStore,
        *,
        hotkey_registered: Callable[[], bool] = lambda: False,
        hotkey_status: Callable[[], str] = lambda: "disabled",
        on_settings_changed: Callable[[dict[str, Any]], None] | None = None,
        on_dock: Callable[[], None] | None = None,
        page_generation: Callable[[], int] = lambda: 0,
    ) -> None:
        self._loop = loop
        self._machine = machine
        self._settings = settings
        self._hotkey_registered = hotkey_registered
        self._hotkey_status = hotkey_status
        self._on_settings_changed = on_settings_changed
        self._on_dock = on_dock
        self._page_generation = page_generation
        self._core_ready = threading.Event()
        if machine is not None:
            self._core_ready.set()
        self.last_heartbeat = time.monotonic()
        # True once the page has called heartbeat() — proof the renderer
        # booted. The shell's watchdog never judges a page before that.
        self.heartbeat_seen = False
        # Set by the page while it holds unsaved work; the shell consults it
        # when the native window is asked to close (fail-open, see app.py).
        self.close_guard = False
        # Status snapshot. One lock, one counter: every observed change to a
        # snapshot field bumps `_revision` exactly once.
        self._status_lock = threading.Lock()
        self._revision = 0
        self._protection: Protection = "unknown"
        self._core_state: CoreState = "ready" if machine is not None else "starting"
        self._core_error: str | None = None
        self._last_status: dict[str, object] | None = None
        # Saves are applied one at a time, each with its settings hook, so a
        # slow hook (layout switch, hotkey) cannot land after a newer save's.
        self._settings_lock = asyncio.Lock()

    def _attach_core(self, machine: SessionManager) -> int:
        """Called once by the shell when the heavy core has been built.
        Returns the status revision to stamp on the push event."""
        self._machine = machine
        with self._status_lock:
            self._core_state = "ready"
            self._core_error = None
            revision = self._observe_locked(None)
        self._core_ready.set()
        return revision

    def _core_failed(self, message: str) -> int:
        """Called once by the shell when building the core raised. Releases
        every command waiting on the core with an actionable error instead of
        a 25 s wait per command followed by a false "still starting". Returns
        the status revision to stamp on the push event."""
        with self._status_lock:
            self._core_state = "failed"
            self._core_error = message
            revision = self._observe_locked(None)
        self._core_ready.set()
        return revision

    def _set_protection(self, protected: bool) -> int:
        """Record a VERIFIED protection verdict; returns the revision the push
        event must carry. An unchanged verdict keeps its revision, so a
        re-emit after a reload is idempotent for the page."""
        with self._status_lock:
            self._protection = "protected" if protected else "unprotected"
            return self._observe_locked(None)

    @property
    def protection(self) -> Protection:
        return self._protection

    @property
    def core_state(self) -> CoreState:
        return self._core_state

    # ------------------------------------------------------------- commands

    def get_settings(self) -> Envelope:
        return self._call(self._get_settings())

    def set_settings(self, patch: object) -> Envelope:
        if not isinstance(patch, dict):
            return _err(AppError("internal", "Settings patch must be an object."))
        return self._call(self._set_settings(patch))

    def start_session(self) -> Envelope:
        machine = self._require_core()
        if machine is None:
            return _err(self._not_ready_error())
        return self._call(machine.start_session(), reap=self._reap_session(machine))

    def stop_session(self, session_id: object) -> Envelope:
        if not isinstance(session_id, str):
            return _err(AppError("internal", "Stop not taken — bad session id."))
        machine = self._require_core()
        if machine is None:
            return _err(self._not_ready_error())
        return self._call(machine.stop_session(session_id))

    def ask(self, text: object) -> Envelope:
        if not isinstance(text, str):
            return _err(AppError("internal", "Type a question first."))
        machine = self._require_core()
        if machine is None:
            return _err(self._not_ready_error())
        return self._call(machine.ask(text), reap=self._reap_session(machine))

    def cancel_session(self, session_id: object) -> Envelope:
        # Fire-and-forget; invalid ids do nothing — never an error.
        machine = self._machine
        if isinstance(session_id, str) and machine is not None:
            self._loop.call_soon_threadsafe(machine.cancel_session, session_id)
        return _ok(None)

    def heartbeat(self) -> Envelope:
        # Renderer liveness ping (crash-recovery fallback).
        self.last_heartbeat = time.monotonic()
        self.heartbeat_seen = True
        return _ok(None)

    def get_status(self) -> Envelope:
        """The page's authoritative snapshot; never waits for the core."""
        session = self._session_snapshot()
        with self._status_lock:
            revision = self._observe_locked(session)
            return _ok(self._status_locked(session, revision))

    def set_close_guard(self, active: object) -> Envelope:
        """The page reports whether closing now would lose unsaved work."""
        self.close_guard = active is True
        return _ok(None)

    def dock_window(self) -> Envelope:
        """Move the window to the top-centre of its display (the camera line).
        Runs synchronously on pywebview's worker thread: it touches no
        session state, and window moves must not wait on the core loop."""
        if self._on_dock is not None:
            with contextlib.suppress(Exception):
                self._on_dock()
        return _ok(None)

    def open_external(self, url: object) -> Envelope:
        # https only; the app never navigates the webview away from its page.
        if isinstance(url, str) and _is_openable_https(url):
            webbrowser.open(url)
            return _ok(None)
        return _err(AppError("internal", "Only https links can be opened."))

    # ------------------------------------------------------------ internals

    def _require_core(self) -> SessionManager | None:
        if self._machine is None:
            self._core_ready.wait(CORE_READY_TIMEOUT_S)
        return self._machine

    def _not_ready_error(self) -> AppError:
        if self._core_state == "failed":
            return AppError(
                "internal",
                "The app core failed to start, so recording and answers are "
                "unavailable. Restart the app; details are in crash.log.",
            )
        return _STILL_STARTING

    def _session_snapshot(self) -> dict[str, object]:
        machine = self._machine
        if machine is None:
            return dict(_IDLE_SESSION)

        async def read() -> dict[str, object]:
            # Read ON the loop: the machine's slot is only mutated there.
            return _page_session(machine.active_snapshot())

        future = asyncio.run_coroutine_threadsafe(read(), self._loop)
        try:
            return future.result(timeout=STATUS_SESSION_TIMEOUT_S)
        except Exception:
            future.cancel()
            return {"id": None, "phase": "unknown"}

    def _status_locked(self, session: dict[str, object], revision: int) -> dict[str, object]:
        return {
            "revision": revision,
            "coreReady": self._core_state == "ready",
            "core": self._core_state,
            "coreError": self._core_error,
            "protection": self._protection,
            "session": session,
            "pageGeneration": self._safe_page_generation(),
        }

    def _observe_locked(self, session: dict[str, object] | None) -> int:
        """Bump the revision iff a snapshot field changed since last observed.
        `session=None` keeps the last observed session: protection and core
        updates arrive off the loop and must not block on it."""
        previous = self._last_status
        if session is None:
            session = (
                previous["session"]  # type: ignore[assignment]
                if previous is not None
                else dict(_IDLE_SESSION)
            )
        fields: dict[str, object] = {
            "core": self._core_state,
            "coreError": self._core_error,
            "protection": self._protection,
            "session": session,
        }
        if fields != previous:
            self._revision += 1
            self._last_status = fields
        return self._revision

    def _safe_page_generation(self) -> int:
        try:
            return int(self._page_generation())
        except Exception:
            return 0

    def _call(
        self,
        coro: Coroutine[object, object, object],
        reap: Callable[[object], None] | None = None,
    ) -> Envelope:
        """Run `coro` on the loop and wait (bounded) for its result.

        `reap` handles a result nobody will receive: when the wait gives up
        (timeout or error) but the command still completes — `future.cancel()`
        cannot stop a coroutine that already finished or is about to — the
        page was told it failed, so whatever it created (a recording
        session) must be undone. Exactly one side reaps: the lock decides
        whether the result landed before or after the caller gave up.
        """
        lock = threading.Lock()
        state: dict[str, Any] = {"abandoned": False, "done": False, "result": None}

        async def guarded() -> object:
            result = await coro
            with lock:
                state["done"], state["result"] = True, result
                abandoned = state["abandoned"]
            if abandoned and reap is not None:
                reap(result)  # on the loop
            return result

        future = asyncio.run_coroutine_threadsafe(guarded(), self._loop)
        try:
            return _ok(future.result(timeout=COMMAND_TIMEOUT_S))
        except AppError as err:
            return _err(err)
        except Exception:
            with lock:
                state["abandoned"] = True
                landed, result = state["done"], state["result"]
            if landed and reap is not None:
                # Finished in the gap between the deadline and now.
                self._loop.call_soon_threadsafe(reap, result)
            future.cancel()
            return _err(AppError("internal", "Something went wrong inside the app core."))

    def _reap_session(self, machine: SessionManager) -> Callable[[object], None]:
        """Cancel ONLY the session a late, unreceived start/ask created."""

        def reap(result: object) -> None:
            if isinstance(result, str):
                machine.cancel_session(result)

        return reap

    def _decorate(self, view: dict[str, Any]) -> dict[str, Any]:
        return {
            **view,
            "hotkeyRegistered": self._hotkey_registered(),
            "hotkeyStatus": self._hotkey_status(),
        }

    async def _get_settings(self) -> dict[str, Any]:
        view = await asyncio.to_thread(self._settings.view)
        return self._decorate(view)

    async def _set_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        # Serialized: each save and its window/hotkey application complete
        # before the next save starts, so side effects land in save order.
        # Ordering against the USER's submission order is the store's
        # `baseRevision` precondition (settings.py): pywebview gives every
        # call its own thread, so arrival order is not submission order.
        async with self._settings_lock:
            view = await asyncio.to_thread(self._settings.patch, patch)
            if self._on_settings_changed is not None:
                # Window/hotkey application must not fail the save — and must
                # not run ON the loop: re-registering the hotkey joins the old
                # message-loop thread and waits for the new one to report,
                # which would stall every event and audio frame meanwhile.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._on_settings_changed, view)
            return self._decorate(view)


_STILL_STARTING = AppError(
    "internal", "The app is still starting up — try again in a moment."
)


def _page_session(active: object) -> dict[str, object]:
    """The page's view of the machine's slot (id + page phase)."""
    if active is None or getattr(active, "aborted", False):
        return dict(_IDLE_SESSION)
    phase = _PAGE_PHASES.get(str(getattr(active, "phase", "")))
    session_id = getattr(active, "id", None)
    if phase is None or not isinstance(session_id, str):
        return dict(_IDLE_SESSION)
    return {"id": session_id, "phase": phase}


def _is_openable_https(url: str) -> bool:
    """An absolute https URL with a host and no whitespace or control
    characters — webbrowser hands the string to the OS shell."""
    if not url.startswith("https://") or len(url) > 2048:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False
