"""AI Call Assistant v3 — Windows entry point.

Wires the core (asyncio loop on a dedicated thread) to the pywebview shell:
single instance, content protection, global hotkey, window-geometry
persistence (per layout mode), the prompter layout and camera-line docking,
crash logging, and renderer crash recovery.

Startup order matters for perceived load time. Everything the window needs
is light (settings, the bridge, the hotkey); everything heavy — httpx and
the provider stack, websockets, numpy and the audio stack — is imported and
assembled by `App._build_core` on a background thread WHILE WebView2 boots
and the page loads. Session commands wait on the core for a bounded time;
the settings screen never needs it.

Two coordinate spaces exist in this file. Before `webview.start()` the
process is DPI-unaware and Win32 reports LOGICAL pixels, which is what
pywebview's `create_window(x=, y=)` takes — so `run()` must read the work
areas BEFORE creating the window. After start, pywebview flips the process
to system-DPI-aware: Win32 reports PHYSICAL pixels while pywebview's
`window.x/y/width/height`, `move()` and `resize()` stay logical, so runtime
geometry divides Win32 rectangles by `_runtime_scale()` first.
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
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from app_core.bridge.api import JsApi
from app_core.bridge.events import WebviewEventSink
from app_core.bridge.hotkey import HotkeyManager
from app_core.bridge.protection import (
    Win32DisplayAffinity,
    apply_content_protection,
)
from app_core.contracts import OnSttError, OnSttUpdate
from app_core.llm.base import default_registry
from app_core.store.bounds import (
    Placement,
    WorkArea,
    plan_restore,
    primary_area,
    top_center,
)
from app_core.store.secrets import DpapiKeystore
from app_core.store.settings import SettingsStore

if TYPE_CHECKING:
    from app_core.session.machine import SessionManager
    from app_core.stt.client import DeepgramStream

APP_NAME = "AICallAssistant"
WINDOW_TITLE = "AI Call Assistant"
# Logical pixels throughout. MIN_SIZE is the create-time envelope for BOTH
# layouts (WinForms enforces it on every later resize), so it must admit the
# prompter strip; the full layout is kept sane by FULL_MIN_HEIGHT at restore.
DEFAULT_SIZE = (460, 700)
PROMPTER_SIZE = (720, 260)
MIN_SIZE = (380, 160)
FULL_MIN_HEIGHT = 520
DOCK_TOP_MARGIN = 8
BACKGROUND = "#16181d"
CONTENT_PROTECTION_ATTEMPTS = 5
CONTENT_PROTECTION_RETRY_S = 0.2
MUTEX_NAME = "Local\\AICallAssistantV3Mutex"
FOCUS_EVENT_NAME = "Local\\AICallAssistantV3Focus"
BOUNDS_DEBOUNCE_S = 0.5
HEARTBEAT_STALE_S = 15.0
RELOAD_COOLDOWN_S = 10.0
RENDERER_PROBE_TIMEOUT_S = 3.0
# A page that has not finished loading is never probed or reloaded until it
# has had this long (cold WebView2 start can be slow); past it, a boot that
# never completed is treated like any other dead renderer.
BOOT_GRACE_S = 60.0
# A close cancelled for unsaved work is let through on the next attempt
# within this window — the window can never become uncloseable.
CLOSE_CONFIRM_WINDOW_S = 10.0
# Budget for the orderly exit (cancel session, close HTTP, drain the audio
# worker). Past it the process exits hard rather than hang on a device call.
SHUTDOWN_TIMEOUT_S = 3.0


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


def crash_log(kind: str, text: str) -> None:
    log = getattr(install_crash_logging, "log_line", None)
    if log is not None:
        log(kind, text)


def loop_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    # One failed pipeline must never take the process down; log and move on.
    message = context.get("message") or ""
    exc = context.get("exception")
    detail = f"{message} {exc!r}" if exc else message
    crash_log("asyncio", detail)


# Windows parks minimized windows at this sentinel coordinate.
MINIMIZED_SENTINEL = -30000


def plausible_bounds(bounds: dict[str, int]) -> bool:
    """False for geometry a minimized (or otherwise unreal) window reports."""
    if bounds["x"] <= MINIMIZED_SENTINEL or bounds["y"] <= MINIMIZED_SENTINEL:
        return False
    return bounds["width"] >= MIN_SIZE[0] and bounds["height"] >= MIN_SIZE[1]


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


def current_work_areas(divisor: float = 1.0) -> list[WorkArea]:
    """Every display's work area. Units are whatever Win32 reports for this
    process (logical before `webview.start()`, physical after); pass the
    runtime scale as `divisor` to get logical units at runtime."""
    import win32api

    areas: list[WorkArea] = []
    try:
        for monitor_handle, _dc, _rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(monitor_handle)
            left, top, right, bottom = info["Work"]
            areas.append(
                WorkArea(
                    left=round(left / divisor),
                    top=round(top / divisor),
                    right=round(right / divisor),
                    bottom=round(bottom / divisor),
                )
            )
    except Exception:
        pass
    return areas


def default_size(mode: str) -> tuple[int, int]:
    return PROMPTER_SIZE if mode == "prompter" else DEFAULT_SIZE


def min_height(mode: str) -> int:
    return MIN_SIZE[1] if mode == "prompter" else FULL_MIN_HEIGHT


def initial_window_kwargs(
    mode: str, stored_raw: object, areas: Sequence[WorkArea]
) -> dict[str, int]:
    """create_window geometry for a layout mode, from that mode's saved bounds.

    A saved position with a reachable title bar is restored verbatim.
    Otherwise the window is docked top-centre on the primary display (the
    camera line) at its saved or default size, shrunk to fit the work area —
    the default 460x700 is taller than a 1280x672 laptop work area and used
    to land under the taskbar. With no display information the OS centres
    it as before.
    """
    placement = plan_mode_geometry(mode, stored_raw, areas, dock_area=None)
    if placement.x is None or placement.y is None:
        return {"width": placement.width, "height": placement.height}
    return {
        "x": placement.x,
        "y": placement.y,
        "width": placement.width,
        "height": placement.height,
    }


def plan_mode_geometry(
    mode: str,
    stored_raw: object,
    areas: Sequence[WorkArea],
    dock_area: WorkArea | None,
) -> Placement:
    """The layout mode's restore decision (pure; see bounds.plan_restore)."""
    return plan_restore(
        stored_raw,
        min_width=MIN_SIZE[0],
        min_height=min_height(mode),
        default_size=default_size(mode),
        work_areas=areas,
        dock_area=dock_area,
        margin=DOCK_TOP_MARGIN,
    )


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
        self._protection_lock = threading.Lock()
        self._protection_seq = 0
        # Renderer boot tracking for the watchdog (see _check_renderer).
        self._boot_started: float | None = None
        self._page_loaded = False
        # The watchdog already advanced the page generation for the page it
        # is loading; its loaded callback must not advance it a second time.
        self._reload_generation_pending = False
        self._close_blocked_at: float | None = None
        self._geometry_lock = threading.RLock()
        self._layout_mode = self.settings.layout_mode()
        self._entry_url = resolve_entry_url()
        # The heavy core arrives from _build_core; see the module docstring.
        self.machine: SessionManager | None = None
        self.http: Any = None
        self.warmer: Any = None
        self._audio_executor: ThreadPoolExecutor | None = None
        self.api = JsApi(
            self.loop,
            None,
            self.settings,
            hotkey_registered=lambda: self.hotkey.registered,
            hotkey_status=lambda: self.hotkey.status,
            on_settings_changed=self._apply_settings,
            on_dock=self._dock_current,
            page_generation=lambda: self.sink.page_generation,
        )

    # ------------------------------------------------------------ loop side

    def start_loop_thread(self) -> None:
        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.set_exception_handler(loop_exception_handler)
            self.loop.call_soon(self.sink.start)
            self.loop.run_forever()

        threading.Thread(target=run, name="core-loop", daemon=True).start()

    def _build_core(self) -> None:
        """Import and assemble the heavy core. Runs on a background thread so
        the window is created and painted while this loads; the machine is
        handed to the bridge on the loop when it is ready."""
        try:
            import httpx

            from app_core.audio.capture import LoopbackCapture
            from app_core.llm.warm import PreWarmer
            from app_core.session.machine import SessionManager
            from app_core.stt.client import DeepgramStream

            with contextlib.suppress(Exception):
                import pyaudiowpatch  # noqa: F401 - warm the import for the first Record

            http = httpx.AsyncClient(
                # ONE shared client for all LLM traffic — its pool is what makes
                # pre-warming work. Generous keepalive so the warmed TLS
                # connection survives until the answer request needs it.
                limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0),
                timeout=httpx.Timeout(10.0, read=75.0),
            )
            warmer = PreWarmer(http)

            def stt_factory(
                key: str, on_update: OnSttUpdate, on_error: OnSttError
            ) -> DeepgramStream:
                return DeepgramStream(key, on_update, on_error)

            # The app owns the audio worker (non-daemon: a device call must
            # not be killed mid-flight) so `shutdown` can drain and stop it.
            audio_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="aica-audio"
            )
            self._audio_executor = audio_executor
            machine = SessionManager(
                settings=self.settings,
                providers=self.registry.as_mapping(),
                stt_factory=stt_factory,
                audio=LoopbackCapture(self.loop),
                http=http,
                warmer=warmer,
                events=self.sink,
                audio_executor=audio_executor,
            )
        except Exception as exc:
            crash_log("core", traceback.format_exc())
            if self._audio_executor is not None:
                self._audio_executor.shutdown(wait=False)
                self._audio_executor = None
            # Terminal, and said out loud: every waiting command is released
            # with this error at once, and the page is told.
            revision = self.api._core_failed(type(exc).__name__)
            self._emit_from_thread(
                "core:failed", {"revision": revision, "error": type(exc).__name__}
            )
            return
        self.http = http
        self.warmer = warmer
        self.machine = machine
        revision = self.api._attach_core(machine)
        self._emit_from_thread("core:ready", {"revision": revision})

    def start_core_thread(self) -> None:
        threading.Thread(target=self._build_core, name="core-build", daemon=True).start()

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
        mode = str(view.get("layoutMode") or "full")
        if mode != self._layout_mode:
            self._switch_layout(mode)

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt32())
        except Exception:
            try:
                import win32gui

                return int(win32gui.FindWindow(None, WINDOW_TITLE))
            except Exception:
                return 0

    # ------------------------------------------------------------- geometry

    def _runtime_scale(self) -> float:
        """Physical-per-logical pixel ratio of the window's display — the same
        number pywebview uses for `window.x/y/move/resize`."""
        try:
            import ctypes

            hwnd = self._hwnd()
            if hwnd:
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                if dpi:
                    return float(dpi) / 96.0
        except Exception:
            pass
        return 1.0

    def _window_work_area(self) -> WorkArea | None:
        """Work area of the display the window is on, in LOGICAL pixels."""
        scale = self._runtime_scale()
        try:
            import win32api
            import win32con

            hwnd = self._hwnd()
            if hwnd:
                monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
                left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
                return WorkArea(
                    left=round(left / scale),
                    top=round(top / scale),
                    right=round(right / scale),
                    bottom=round(bottom / scale),
                )
        except Exception:
            pass
        return primary_area(current_work_areas(divisor=scale))

    def _place(self, x: int | None, y: int | None, width: int, height: int) -> None:
        """Resize then move, in logical pixels. A None position only resizes
        (no display information to place against). Never raises."""
        window = self.window
        if window is None:
            return
        with contextlib.suppress(Exception):
            window.resize(int(width), int(height))
        if x is None or y is None:
            return
        with contextlib.suppress(Exception):
            window.move(int(x), int(y))

    def _dock_top_center(self, width: int, height: int) -> None:
        area = self._window_work_area()
        if area is None:
            return
        self._place(*top_center(area, width, height, DOCK_TOP_MARGIN))

    def _dock_current(self) -> None:
        """Dock the window under the camera at its current size (js_api)."""
        window = self.window
        if window is None:
            return
        with self._geometry_lock:
            try:
                width, height = int(window.width), int(window.height)
            except Exception:
                width, height = default_size(self._layout_mode)
            self._dock_top_center(width, height)

    def _switch_layout(self, mode: str) -> None:
        """Swap between the full panel and the prompter strip: flush the old
        mode's geometry under its own key, then restore the new mode's saved
        geometry or dock it under the camera at its default size."""
        with self._geometry_lock:
            if self._bounds_timer is not None:
                self._bounds_timer.cancel()
            self._save_bounds()
            # Set BEFORE moving so the moved/resized callbacks the move
            # triggers save under the new key.
            self._layout_mode = mode
            # Validate against EVERY display: the full panel saved on display
            # A is still exactly restorable while the prompter sits on B. The
            # current display is only the fallback dock target.
            placement = plan_mode_geometry(
                mode,
                self.settings.window_bounds(mode=mode),
                current_work_areas(divisor=self._runtime_scale()),
                dock_area=self._window_work_area(),
            )
            self._place(placement.x, placement.y, placement.width, placement.height)

    # ----------------------------------------------------------- protection

    def _apply_content_protection(self) -> None:
        """Invisible to screen sharing — the moat feature. Applied AND
        verified: a window the user believes is hidden while it is being
        broadcast is the worst outcome this app has, so a failure that
        survives the retries is reported to them rather than logged."""
        # `shown` and `loaded` both trigger this, so attempts overlap. Stamp
        # each one: a slow loser that finishes after a newer attempt already
        # reported must not overwrite the fresher verdict with a stale one.
        with self._protection_lock:
            self._protection_seq += 1
            attempt_id = self._protection_seq
        protected = False
        for attempt in range(CONTENT_PROTECTION_ATTEMPTS):
            if apply_content_protection(self._hwnd(), self._affinity_api):
                protected = True
                break
            # The HWND may not be ready in the first instants after `shown`.
            time.sleep(CONTENT_PROTECTION_RETRY_S * (attempt + 1))
        with self._protection_lock:
            if attempt_id != self._protection_seq:
                return  # superseded by a newer attempt; its verdict stands
            self._protection_failed = not protected
            # The verdict joins the status snapshot (get_status) under the
            # same revision the event carries, so a page that reads the
            # snapshot and hears the event can tell which is newer. Emitted
            # under the lock so two verdicts reach the queue in order.
            revision = self.api._set_protection(protected)
            # Always emit, never only on transition: this also runs after a
            # renderer reload, where the page has lost the previous state.
            self._emit_from_thread(
                "protection:ok" if protected else "protection:failed",
                {
                    "revision": revision,
                    "protection": "protected" if protected else "unprotected",
                },
            )

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
        # The page starts booting now. The watchdog does not judge it until
        # it has loaded (or heartbeated), or BOOT_GRACE_S has passed — a
        # clock reset alone cannot protect a slow cold start (WebView2
        # environment creation, the first page load) from a probe.
        now = time.monotonic()
        if self._boot_started is None:
            self._boot_started = now
        self.api.last_heartbeat = now
        self._protect_async()

    def _on_loaded(self) -> None:
        self._page_loaded = True
        self.api.last_heartbeat = time.monotonic()
        # A fresh page: stamp later events with its generation, and forget
        # the dead page's unsaved-work flag (the new page re-reports it).
        # After a watchdog reload the generation was advanced before
        # load_url; a second bump here would make the page drop events that
        # were dispatched in the load window and delivered to it.
        if self._reload_generation_pending:
            self._reload_generation_pending = False
        else:
            self.sink.new_page()
        self.api.close_guard = False
        self._protect_async()

    def _renderer_booted(self) -> bool:
        return self._page_loaded or self.api.heartbeat_seen

    # ------------------------------------------------------------- geometry

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
            with self._geometry_lock:
                window = self.window
                if window is None:
                    return
                bounds = {
                    "x": int(window.x),
                    "y": int(window.y),
                    "width": int(window.width),
                    "height": int(window.height),
                }
                if not plausible_bounds(bounds):
                    # Minimizing reports Location (-32000, -32000) and a
                    # titlebar-sized Size; persisting that loses the geometry
                    # the user actually arranged the moment they minimize.
                    return
                self.settings.set_window_bounds(bounds, mode=self._layout_mode)
        except Exception:
            pass  # geometry is cosmetic — never raise, especially at shutdown

    def _on_closing(self) -> bool:
        """pywebview `closing`: returning False cancels the close.

        The close is held back ONCE when the page has reported unsaved work
        and is demonstrably alive, so it can ask the user; it emits
        `window:close-requested` for that. It always proceeds when the page
        is not answering (it could never ask), and on any second attempt
        within CLOSE_CONFIRM_WINDOW_S — the window can never become
        uncloseable, whatever the page does.
        """
        if self._bounds_timer is not None:
            self._bounds_timer.cancel()
        self._save_bounds()
        now = time.monotonic()
        if self._should_hold_close(now):
            self._close_blocked_at = now
            self._emit_from_thread("window:close-requested", {})
            return False
        return True

    def _should_hold_close(self, now: float) -> bool:
        if not self.api.close_guard or not self._renderer_booted():
            return False
        if now - self.api.last_heartbeat > HEARTBEAT_STALE_S:
            return False  # a page that is not answering cannot ask anyone
        blocked_at = self._close_blocked_at
        return blocked_at is None or now - blocked_at > CLOSE_CONFIRM_WINDOW_S

    # ------------------------------------------------------------- watchdog

    def _probe_renderer(self, window: Any) -> bool:
        """Ask the page directly whether it is alive.

        A stale heartbeat alone is not proof of death: Chromium throttles
        timers in a hidden page — once per second when hidden, once per
        MINUTE after five minutes minimized — so the 3 s heartbeat starves
        while the page is perfectly healthy. Script execution is not
        throttled the same way, so a live renderer answers this promptly;
        a dead one raises or never answers.
        """
        answered: list[object] = []

        def run() -> None:
            with contextlib.suppress(Exception):
                answered.append(window.evaluate_js("1"))

        thread = threading.Thread(target=run, name="renderer-probe", daemon=True)
        thread.start()
        thread.join(RENDERER_PROBE_TIMEOUT_S)
        return bool(answered)

    def _check_renderer(self, now: float) -> bool:
        """One watchdog tick. True when the page was reloaded."""
        window = self.window
        if window is None:
            return False
        if not self._renderer_booted():
            # Still booting (or never shown): a probe here would block in
            # pywebview's own wait for the page and misread the boot as a
            # death. Only a boot that outlives the grace period is judged.
            started = self._boot_started
            if started is None or now - started <= BOOT_GRACE_S:
                return False
        elif now - self.api.last_heartbeat <= HEARTBEAT_STALE_S:
            return False
        if now - self._last_reload <= RELOAD_COOLDOWN_S:
            return False
        if self._probe_renderer(window):
            # Alive, just throttled (minimized). Reloading would wipe the
            # history and every unsent question for nothing. A page that runs
            # script has booted, even if its `loaded` callback was missed.
            self.api.last_heartbeat = now
            self._page_loaded = True
            return False
        # WebView2 renderer died: reload, at most once per 10 s so a
        # boot-crash doesn't flicker forever.
        self._last_reload = now
        self.api.last_heartbeat = now
        # The reloaded page boots from scratch: re-arm the boot gate so it is
        # not probed (and reloaded again) before it can finish loading.
        self._page_loaded = False
        self.api.heartbeat_seen = False
        self._boot_started = now
        # New generation BEFORE the new page exists: a dispatch abandoned on
        # the dead page carries the old one, so the new page can drop it even
        # if it reads get_status() before our `loaded` callback runs.
        self.sink.new_page()
        self._reload_generation_pending = True
        with contextlib.suppress(Exception):
            window.load_url(self._entry_url)
        return True

    def _heartbeat_watchdog(self) -> None:
        def run() -> None:
            while True:
                time.sleep(2.0)
                self._check_renderer(time.monotonic())

        threading.Thread(target=run, name="heartbeat-watchdog", daemon=True).start()

    # ------------------------------------------------------------- lifecycle

    def _wire_window(self, window: Any) -> None:
        """Adopt the window: attach the event sink, then subscribe callbacks.

        The sink attach is load-bearing. Everything the core produces —
        transcript, answer deltas, audio level, errors — reaches the page
        only through it, and the pump parks on `while _window is None` until
        it happens. Commands travel a separate channel (js_api), so without
        this the app looks alive while showing nothing at all.
        """
        self.window = window
        self.sink.attach(window)
        window.events.shown += self._on_shown
        window.events.loaded += self._on_loaded
        window.events.moved += lambda *_a: self._schedule_bounds_save()
        window.events.resized += lambda *_a: self._schedule_bounds_save()
        window.events.closing += lambda *_a: self._on_closing()

    def run(self) -> bool:
        """Run the window until it closes; returns the shutdown verdict."""
        import webview

        view = self.settings.view()
        # BEFORE create_window: the process is still DPI-unaware here, so
        # these work areas are in the logical units create_window expects.
        kwargs: dict[str, Any] = dict(
            initial_window_kwargs(
                self._layout_mode,
                self.settings.window_bounds(mode=self._layout_mode),
                current_work_areas(),
            )
        )
        self._wire_window(
            webview.create_window(
                WINDOW_TITLE,
                url=self._entry_url,
                js_api=self.api,
                min_size=MIN_SIZE,
                background_color=BACKGROUND,
                on_top=bool(view.get("alwaysOnTop", True)),
                **kwargs,
            )
        )
        # Heavy imports overlap WebView2's own boot from here on.
        self.start_core_thread()
        watch_focus_signal(self._hwnd)
        accelerator = str(view.get("hotkey") or "")
        self._current_accelerator = accelerator
        if accelerator:
            self.hotkey.register(accelerator)
        self.start_loop_thread()
        self._heartbeat_watchdog()
        webview.start(gui="edgechromium", debug=bool(os.environ.get("AICA_DEBUG")))
        return self.shutdown()

    def shutdown(self, timeout_s: float | None = None) -> bool:
        """Orderly exit after the window closed, in dependency order: cancel
        the live session (which queues its capture stop on the audio worker),
        close the shared HTTP client, then let the audio worker finish what
        is queued and stop it. Bounded; False means something did not finish
        in time and the caller must exit hard — the audio worker is a
        non-daemon thread, and a hung device call would otherwise keep the
        process alive with no window."""
        budget = SHUTDOWN_TIMEOUT_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget
        clean = True
        machine = self.machine
        if machine is not None and self.loop.is_running():

            async def stop_core() -> None:
                active = machine.active_snapshot()
                if active is not None:
                    machine.cancel_session(active.id)
                if self.http is not None:
                    with contextlib.suppress(Exception):
                        await self.http.aclose()

            future = asyncio.run_coroutine_threadsafe(stop_core(), self.loop)
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                future.cancel()
                clean = False
        executor = self._audio_executor
        if executor is not None:
            # A marker queued behind the session's stop: when it runs, every
            # device call before it has finished.
            marker = executor.submit(lambda: None)
            try:
                marker.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                clean = False
            executor.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):
            self.hotkey.unregister()
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.loop.stop)
        return clean


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
    if not App().run():
        # A device call is hung on the non-daemon audio worker: interpreter
        # shutdown would join it forever. Settings writes are synchronous and
        # already landed, so a hard exit loses nothing.
        crash_log("shutdown", "audio worker did not stop in time; exiting hard")
        os._exit(0)


if __name__ == "__main__":
    main()
