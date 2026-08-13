"""Content protection must be VERIFIED, not merely requested.

Being invisible to screen sharing is the product's moat feature. The Win32
call returns a BOOL that is trivially ignored and can genuinely fail (an HWND
that is not ready, policy, a Windows build older than 2004). A silent failure
ships a window the user believes is hidden while it is being broadcast — so
these tests pin "only a read-back counts as success".
"""

from __future__ import annotations

from app_core.bridge.protection import (
    WDA_EXCLUDEFROMCAPTURE,
    WDA_MONITOR,
    WDA_NONE,
    apply_content_protection,
)

HWND = 0x1234


class FakeAffinity:
    """Records what was requested and reports what the OS "actually" holds."""

    def __init__(
        self,
        *,
        set_ok: bool = True,
        stored: int | None = None,
        raises: bool = False,
    ) -> None:
        self.set_ok = set_ok
        self.stored = stored
        self.raises = raises
        self.set_calls: list[tuple[int, int]] = []

    def set_affinity(self, hwnd: int, affinity: int) -> bool:
        if self.raises:
            raise OSError("user32 exploded")
        self.set_calls.append((hwnd, affinity))
        if self.set_ok:
            self.stored = affinity
        return self.set_ok

    def get_affinity(self, hwnd: int) -> int | None:
        if self.raises:
            raise OSError("user32 exploded")
        return self.stored


class TestApplyContentProtection:
    def test_success_requires_the_os_to_confirm(self) -> None:
        api = FakeAffinity()
        assert apply_content_protection(HWND, api) is True
        assert api.set_calls == [(HWND, WDA_EXCLUDEFROMCAPTURE)]

    def test_set_that_reports_success_but_did_not_stick_is_a_failure(self) -> None:
        # The whole point of reading back: a call that returns TRUE while the
        # affinity stays NONE would otherwise look like protection.
        api = FakeAffinity(set_ok=False, stored=WDA_NONE)
        assert apply_content_protection(HWND, api) is False

    def test_partial_protection_is_not_protection(self) -> None:
        # WDA_MONITOR hides from some capture paths but not the ones that
        # matter for screen sharing; only EXCLUDEFROMCAPTURE counts.
        api = FakeAffinity(set_ok=False, stored=WDA_MONITOR)
        assert apply_content_protection(HWND, api) is False

    def test_unreadable_affinity_is_a_failure(self) -> None:
        api = FakeAffinity(set_ok=False, stored=None)
        assert apply_content_protection(HWND, api) is False

    def test_missing_hwnd_fails_without_calling_the_os(self) -> None:
        api = FakeAffinity()
        assert apply_content_protection(0, api) is False
        assert api.set_calls == []

    def test_a_throwing_api_fails_closed_instead_of_crashing_the_window_callback(
        self,
    ) -> None:
        api = FakeAffinity(raises=True)
        assert apply_content_protection(HWND, api) is False

    def test_constants_match_the_win32_values(self) -> None:
        assert WDA_NONE == 0x00000000
        assert WDA_MONITOR == 0x00000001
        assert WDA_EXCLUDEFROMCAPTURE == 0x00000011
