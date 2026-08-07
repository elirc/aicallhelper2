"""Global hotkey via Win32 RegisterHotKey on a dedicated message-loop thread.

Implemented directly over user32 (ctypes) instead of a wrapper package
because the product contract demands three things wrappers tend to fumble:
fire while ANY app has focus, register/unregister at runtime, and report
registration failure (key taken by another app) rather than silently doing
nothing — the UI shows an honest "hotkey taken" notice off that result.

RegisterHotKey is thread-bound, so each registration runs its own thread
with a GetMessage loop; unregistering posts WM_QUIT to that thread.

The accelerator parser is pure and importable for tests.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import threading
from collections.abc import Callable
from dataclasses import dataclass

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "option": MOD_ALT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
    "cmd": MOD_WIN,
    "cmdorctrl": MOD_CONTROL,
    "commandorcontrol": MOD_CONTROL,
}

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "plus": 0xBB,
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
    "`": 0xC0,
    "~": 0xC0,
}


@dataclass(frozen=True)
class ParsedAccelerator:
    modifiers: int
    vk: int


def parse_accelerator(accelerator: str) -> ParsedAccelerator | None:
    """"Ctrl+Shift+Space" -> (MOD_CONTROL|MOD_SHIFT, VK_SPACE). None = invalid."""
    parts = [p.strip().lower() for p in accelerator.split("+") if p.strip()]
    if not parts:
        return None
    modifiers = 0
    vk: int | None = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        elif vk is not None:
            return None  # two non-modifier keys
        elif len(part) == 1 and (part.isalnum() or part in _NAMED_KEYS):
            vk = _NAMED_KEYS.get(part, ord(part.upper()))
        elif part in _NAMED_KEYS:
            vk = _NAMED_KEYS[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            return None
    if vk is None:
        return None
    return ParsedAccelerator(modifiers=modifiers, vk=vk)


class HotkeyManager:
    """Owns at most one registered global hotkey at a time."""

    def __init__(self, on_fire: Callable[[], None]) -> None:
        self._on_fire = on_fire
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._registered = False
        self._lock = threading.Lock()

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, accelerator: str) -> bool:
        """Register (replacing any current hotkey). Empty accelerator =
        shortcut disabled. Returns True only on real OS-level success."""
        with self._lock:
            self._unregister_locked()
            if not accelerator:
                return False
            parsed = parse_accelerator(accelerator)
            if parsed is None:
                return False
            result: dict[str, bool] = {}
            ready = threading.Event()

            def run() -> None:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                self._thread_id = kernel32.GetCurrentThreadId()
                ok = bool(
                    user32.RegisterHotKey(
                        None, 1, parsed.modifiers | MOD_NOREPEAT, parsed.vk
                    )
                )
                result["ok"] = ok
                ready.set()
                if not ok:
                    return
                try:
                    msg = ctypes.wintypes.MSG()
                    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                        if msg.message == WM_HOTKEY:
                            with contextlib.suppress(Exception):
                                self._on_fire()
                finally:
                    user32.UnregisterHotKey(None, 1)

            thread = threading.Thread(target=run, name="hotkey", daemon=True)
            thread.start()
            ready.wait(timeout=5.0)
            if result.get("ok"):
                self._thread = thread
                self._registered = True
                return True
            self._thread = None
            self._thread_id = None
            self._registered = False
            return False

    def unregister(self) -> None:
        with self._lock:
            self._unregister_locked()

    def _unregister_locked(self) -> None:
        if self._thread is not None and self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None
        self._registered = False
