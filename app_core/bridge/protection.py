"""Content protection: hide the window from screen capture, verifiably.

Being invisible to screen sharing is the product's moat feature — the user
is on a call and may share their screen at any moment. `SetWindowDisplayAffinity`
returns a BOOL that is easy to ignore, and it CAN fail (an HWND that isn't
ready yet, a policy or driver that refuses, Windows builds older than
2004 which lack WDA_EXCLUDEFROMCAPTURE entirely). Ignoring the result means
shipping a window the user believes is hidden while it is being broadcast.

So: apply, then VERIFY by reading the affinity back, and let the caller tell
the user when the OS would not confirm it.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Protocol

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011


class DisplayAffinityApi(Protocol):
    def set_affinity(self, hwnd: int, affinity: int) -> bool: ...

    def get_affinity(self, hwnd: int) -> int | None: ...


class Win32DisplayAffinity:
    """The real user32 calls."""

    def set_affinity(self, hwnd: int, affinity: int) -> bool:
        return bool(
            ctypes.windll.user32.SetWindowDisplayAffinity(
                wintypes.HWND(hwnd), wintypes.DWORD(affinity)
            )
        )

    def get_affinity(self, hwnd: int) -> int | None:
        value = wintypes.DWORD()
        ok = ctypes.windll.user32.GetWindowDisplayAffinity(
            wintypes.HWND(hwnd), ctypes.byref(value)
        )
        return value.value if ok else None


def apply_content_protection(hwnd: int, api: DisplayAffinityApi) -> bool:
    """Exclude the window from capture. True ONLY when the OS confirms it.

    A False here is a user-visible fact, not a log line: the window they
    think is hidden is not.
    """
    if not hwnd:
        return False
    try:
        api.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        return api.get_affinity(hwnd) == WDA_EXCLUDEFROMCAPTURE
    except Exception:
        return False
