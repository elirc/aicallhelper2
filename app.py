"""AI Call Assistant v3 — Windows entry point.

Wires the core (asyncio loop on a dedicated thread) to the pywebview shell:
single instance, content protection, global hotkey, window-geometry
persistence, crash logging, and renderer crash recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from app_core.bridge.api import JsApi
from app_core.bridge.events import WebviewEventSink
from app_core.bridge.hotkey import HotkeyManager
from app_core.bridge.protection import (
    Win32DisplayAffinity,
    apply_content_protection,
)
from app_core.llm.base import default_registry
from app_core.llm.warm import PreWarmer
from app_core.session.machine import OnSttError, OnSttUpdate, SessionManager
from app_core.store.bounds import WorkArea, sanitize_bounds
from app_core.store.secrets import DpapiKeystore
from app_core.store.settings import SettingsStore
from app_core.stt.client import DeepgramStream

APP_NAME = "AICallAssistant"
WINDOW_TITLE = "AI Call Assistant"
DEFAULT_SIZE = (460, 700)
MIN_SIZE = (380, 520)
BACKGROUND = "#16181d"
CONTENT_PROTECTION_ATTEMPTS = 5
CONTENT_PROTECTION_RETRY_S = 0.2
MUTEX_NAME = "Local\\AICallAssistantV3Mutex"
FOCUS_EVENT_NAME = "Local\\AICallAssistantV3Focus"
BOUNDS_DEBOUNCE_S = 0.5
HEARTBEAT_STALE_S = 15.0
RELOAD_COOLDOWN_S = 10.0


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------- crash log


CRASH_LOG_MAX_BYTES = 1_000_000


def install_crash_logging(data_dir: Path) -> None:
    log_path = data_dir / "crash.log"
    try:
        # Rotate once at boot so the log can't grow without bound.
        if log_path.exists() and log_path.stat().st_size > CRASH_LOG_MAX_BYTES:
            log_path.replace(data_dir / "crash.log.1")
    except OSError:
        pass
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
    faulthandler.enable(file=handle)

    def log_line(kind: str, text: str) -> None:
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            handle.write(f"[{stamp}] {kind}: {text}\n")
            handle.flush()
        except Exception:
            pass

    def excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        log_line("unhandled", "".join(traceback.format_exception(exc_type, exc, tb)))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log_line(
            "thread",
            "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            ),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    install_crash_logging.log_line = log_line  # type: ignore[attr-defined]


def loop_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    # One failed pipeline must never take the process down; log and move on.
    message = context.get("message") or ""
    exc = context.get("exception")
    detail = f"{message} {exc!r}" if exc else message
    log = getattr(install_crash_logging, "log_line", None)
    if log is not None:
        log("asyncio", detail)


# ---------------------------------------------------------- single instance


def acquire_single_instance() -> bool:
    """True if we are the first instance. Otherwise signal the first and quit."""
    import win32api
    import win32event
    import winerror

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    acquire_single_instance.handle = handle  # type: ignore[attr-defined] # keep alive
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        focus_evt = win32event.CreateEvent(None, False, False, FOCUS_EVENT_NAME)
        win32event.SetEvent(focus_evt)
        return False
    return True


def watch_focus_signal(get_hwnd: Any) -> None:
    """First instance: focus/restore our window when a second launch signals."""
    import win32con
    import win32event
    import win32gui

    focus_evt = win32event.CreateEvent(None, False, False, FOCUS_EVENT_NAME)

    def run() -> None:
        while True:
            win32event.WaitForSingleObject(focus_evt, 0xFFFFFFFF)
            try:
                hwnd = get_hwnd()
                if hwnd:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass

    threading.Thread(target=run, name="focus-signal", daemon=True).start()


# ----------------------------------------------------------------- monitors


def current_work_areas() -> list[WorkArea]:
    import win32api

    areas: list[WorkArea] = []
    try:
        for monitor_handle, _dc, _rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(monitor_handle)
            left, top, right, bottom = info["Work"]
            areas.append(WorkArea(left=left, top=top, right=right, bottom=bottom))
    except Exception:
        pass
    return areas


# --------------------------------------------------------------------- app


class App:
    def __init__(self) -> None:
        self.data_dir = app_data_dir()
        self.loop = asyncio.new_event_loop()
        self.registry = default_registry()
        self.settings = SettingsStore(
            self.data_dir / "settings.json", DpapiKeystore(), self.registry
        )
        self.sink = WebviewEventSink()
        self.window: Any = None
        self.hotkey = HotkeyManager(self._on_hotkey)
        self._bounds_timer: threading.Timer | None = None
        self._last_reload = 0.0
        self._current_accelerator: str | None = None
        self._affinity_api = Win32DisplayAffinity()
        self._protection_failed = False
        self._entry_url = resolve_entry_url()

        self.http = httpx.AsyncClient(
            # ONE shared client for all LLM traffic — its pool is what makes
            # pre-warming work. Generous keepalive so the warmed TLS
            # connection survives until the answer request needs it.
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0),
            timeout=httpx.Timeout(10.0, read=75.0),
        )
        self.warmer = PreWarmer(self.http)

        def stt_factory(
            key: str, on_update: OnSttUpdate, on_error: OnSttError
        ) -> DeepgramStream:
            return DeepgramStream(key, on_update, on_error)

        from app_core.audio.capture import LoopbackCapture

        self.machine = SessionManager(
            settings=self.settings,
            providers=self.registry.as_mapping(),
            stt_factory=stt_factory,
            audio=LoopbackCapture(self.loop),
            http=self.http,
            warmer=self.warmer,
            events=self.sink,
        )
        self.api = JsApi(
            self.loop,
            self.machine,
            self.settings,
            hotkey_registered=lambda: self.hotkey.registered,
            hotkey_status=lambda: self.hotkey.status,
            on_settings_changed=self._apply_settings,
        )

    # ------------------------------------------------------------ loop side

    def start_loop_thread(self) -> None:
        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.set_exception_handler(loop_exception_handler)
            self.loop.call_soon(self.sink.start)
            self.loop.run_forever()

        threading.Thread(target=run, name="core-loop", daemon=True).start()

    # ------------------------------------------------------------- UI side

    def _on_hotkey(self) -> None:
        self.loop.call_soon_threadsafe(self.sink.emit, "hotkey:toggle", {})

    def _apply_settings(self, view: dict[str, Any]) -> None:
        accelerator = str(view.get("hotkey") or "")
        # Re-registering an unchanged hotkey briefly unbinds it (and can lose
        # it to another app in the gap) — only touch the OS when it changed.
        if accelerator != self._current_accelerator:
            self.hotkey.register(accelerator)
            self._current_accelerator = accelerator
        if self.window is not None:
            with contextlib.suppress(Exception):
                self.window.on_top = bool(view.get("alwaysOnTop", True))

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt32())
        except Exception:
            try:
                import win32gui

                return int(win32gui.FindWindow(None, WINDOW_TITLE))
            except Exception:
                return 0

    def _apply_content_protection(self) -> None:
        """Invisible to screen sharing — the moat feature. Applied AND
        verified: a window the user believes is hidden while it is being
        broadcast is the worst outcome this app has, so a failure that
        survives the retries is reported to them rather than logged."""
        protected = False
        for attempt in range(CONTENT_PROTECTION_ATTEMPTS):
            if apply_content_protection(self._hwnd(), self._affinity_api):
                protected = True
                break
            # The HWND may not be ready in the first instants after `shown`.
            time.sleep(CONTENT_PROTECTION_RETRY_S * (attempt + 1))
        self._protection_failed = not protected
        # Always emit, never only on transition: this also runs after a
        # renderer reload, where the page has lost the previous state.
        self._emit_from_thread("protection:ok" if protected else "protection:failed", {})

    def _emit_from_thread(self, name: str, payload: dict[str, Any]) -> None:
        # Window callbacks run on pywebview's threads; the sink queue lives
        # on the core loop.
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.sink.emit, name, payload)

    def _protect_async(self) -> None:
        # Never block a pywebview event callback on the retry sleeps.
        threading.Thread(
            target=self._apply_content_protection, name="content-protection", daemon=True
        ).start()

    def _on_shown(self) -> None:
        self._protect_async()

    def _on_loaded(self) -> None:
        self._protect_async()

    def _schedule_bounds_save(self) -> None:
        # Debounced on move/resize AND flushed on close: the debounced save
        # is what makes crash/kill keep the geometry.
        if self._bounds_timer is not None:
            self._bounds_timer.cancel()
        self._bounds_timer = threading.Timer(BOUNDS_DEBOUNCE_S, self._save_bounds)
        self._bounds_timer.daemon = True
        self._bounds_timer.start()

    def _save_bounds(self) -> None:
        try:
            window = self.window
            if window is None:
                return
            bounds = {
                "x": int(window.x),
                "y": int(window.y),
                "width": int(window.width),
                "height": int(window.height),
            }
            self.settings.set_window_bounds(bounds)
        except Exception:
            pass  # geometry is cosmetic — never raise, especially at shutdown

    def _on_closing(self) -> None:
        if self._bounds_timer is not None:
            self._bounds_timer.cancel()
        self._save_bounds()

    def _heartbeat_watchdog(self) -> None:
        def run() -> None:
            while True:
                time.sleep(2.0)
                window = self.window
                if window is None:
                    continue
                stale = time.monotonic() - self.api.last_heartbeat
                if stale > HEARTBEAT_STALE_S and (
                    time.monotonic() - self._last_reload > RELOAD_COOLDOWN_S
                ):
                    # WebView2 renderer died: reload, at most once per 10 s so
                    # a boot-crash doesn't flicker forever.
                    self._last_reload = time.monotonic()
                    self.api.last_heartbeat = time.monotonic()
                    with contextlib.suppress(Exception):
                        window.load_url(self._entry_url)

        threading.Thread(target=run, name="heartbeat-watchdog", daemon=True).start()

    def run(self) -> None:
        import webview

        view = self.settings.view()
        stored = sanitize_bounds(
            self.settings.window_bounds(),
            min_width=MIN_SIZE[0],
            min_height=MIN_SIZE[1],
            work_areas=current_work_areas(),
        )
        kwargs: dict[str, Any] = {}
        if stored is not None:
            kwargs["width"] = stored.width
            kwargs["height"] = stored.height
            if stored.x is not None and stored.y is not None:
                kwargs["x"] = stored.x
                kwargs["y"] = stored.y
        else:
            kwargs["width"], kwargs["height"] = DEFAULT_SIZE

        self.window = webview.create_window(
            WINDOW_TITLE,
            url=self._entry_url,
            js_api=self.api,
            min_size=MIN_SIZE,
            background_color=BACKGROUND,
            on_top=bool(view.get("alwaysOnTop", True)),
            **kwargs,
        )
        self.window.events.shown += self._on_shown
        self.window.events.loaded += self._on_loaded
        self.window.events.moved += lambda *_a: self._schedule_bounds_save()
        self.window.events.resized += lambda *_a: self._schedule_bounds_save()
        self.window.events.closing += lambda *_a: self._on_closing()

        watch_focus_signal(self._hwnd)
        accelerator = str(view.get("hotkey") or "")
        self._current_accelerator = accelerator
        if accelerator:
            self.hotkey.register(accelerator)
        self.start_loop_thread()
        self._heartbeat_watchdog()
        webview.start(gui="edgechromium", debug=bool(os.environ.get("AICA_DEBUG")))


def resolve_entry_url() -> str:
    dev_url = os.environ.get("AICA_DEV_URL")
    if dev_url:
        return dev_url
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    dist = base / "frontend" / "dist" / "index.html"
    return str(dist)


def main() -> None:
    data_dir = app_data_dir()
    install_crash_logging(data_dir)
    if not acquire_single_instance():
        return  # second launch: first instance was signalled to focus
    App().run()


if __name__ == "__main__":
    main()
