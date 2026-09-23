"""Accelerator parsing (pure — no OS registration in tests)."""

from __future__ import annotations

import pytest

from app_core.bridge.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyManager,
    parse_accelerator,
)


class TestParseAccelerator:
    def test_default_hotkey(self) -> None:
        parsed = parse_accelerator("Ctrl+Shift+Space")
        assert parsed is not None
        assert parsed.modifiers == MOD_CONTROL | MOD_SHIFT
        assert parsed.vk == 0x20

    def test_case_and_spacing_tolerant(self) -> None:
        assert parse_accelerator("ctrl + shift + space") == parse_accelerator(
            "CTRL+SHIFT+SPACE"
        )

    def test_letter_and_digit_keys(self) -> None:
        parsed = parse_accelerator("Alt+R")
        assert parsed is not None and parsed.modifiers == MOD_ALT and parsed.vk == ord("R")
        parsed = parse_accelerator("Ctrl+1")
        assert parsed is not None and parsed.vk == ord("1")

    def test_function_keys(self) -> None:
        parsed = parse_accelerator("F1")
        assert parsed is not None and parsed.vk == 0x70
        parsed = parse_accelerator("Ctrl+F12")
        assert parsed is not None and parsed.vk == 0x7B

    def test_win_modifier(self) -> None:
        parsed = parse_accelerator("Win+Space")
        assert parsed is not None and parsed.modifiers == MOD_WIN

    def test_invalid_forms(self) -> None:
        assert parse_accelerator("") is None
        assert parse_accelerator("+") is None
        assert parse_accelerator("Ctrl+Shift") is None  # no key
        assert parse_accelerator("Ctrl+Foo") is None
        assert parse_accelerator("A+B") is None  # two keys
        assert parse_accelerator("F25") is None


class TestHostileAccelerators:
    def test_multi_char_uppercase_returns_none_instead_of_raising(self) -> None:
        # "ß".upper() == "SS", which used to make ord() raise TypeError. That
        # escaped hotkey registration at launch and bricked startup until
        # settings.json was hand-edited.
        assert parse_accelerator("ctrl+ß") is None
        assert parse_accelerator("ﬁ") is None

    def test_non_ascii_keys_rejected_rather_than_mapped_to_bogus_vks(self) -> None:
        # Win32 VK codes are ASCII-based; ord("é") == 0xC9 is unassigned and
        # ord("日") is out of range entirely.
        assert parse_accelerator("ctrl+é") is None
        assert parse_accelerator("ctrl+日") is None
        assert parse_accelerator("ctrl+€") is None

    def test_ascii_keys_still_work(self) -> None:
        assert parse_accelerator("ctrl+a") is not None
        assert parse_accelerator("ctrl+9") is not None

    def test_no_accelerator_input_ever_raises(self) -> None:
        for candidate in ["ß", "+++", "ctrl+", "ctrl++", "\x00", "🙂", "ctrl+🙂", " " * 5]:
            parse_accelerator(candidate)  # must not raise


class TestRegistrationStatus:
    """"Invalid" and "taken by another app" are different user problems; the
    UI shows a different message for each, so the manager must distinguish
    them rather than returning one bare False."""

    def test_empty_accelerator_is_disabled_not_a_failure(self) -> None:
        manager = HotkeyManager(lambda: None)
        assert manager.register("") == "disabled"
        assert manager.status == "disabled"
        assert manager.registered is False

    def test_unparseable_accelerator_reports_invalid(self) -> None:
        manager = HotkeyManager(lambda: None)
        for bad in ["Ctrl+Foo", "Ctrl+Shift", "A+B", "ctrl+ß", "F25"]:
            assert manager.register(bad) == "invalid", bad
            assert manager.status == "invalid"
            assert manager.registered is False

    def test_a_real_registration_reports_registered_and_a_conflict_unavailable(
        self,
    ) -> None:
        # Ctrl+Alt+Shift+F24 is not a shortcut anything else claims.
        first = HotkeyManager(lambda: None)
        try:
            status = first.register("Ctrl+Alt+Shift+F24")
            if status != "registered":
                pytest.skip("the OS refused the probe hotkey in this environment")
            assert first.registered is True
            second = HotkeyManager(lambda: None)
            try:
                # Win32 RegisterHotKey is per-process; a second manager in the
                # SAME process contends for the same id, which is exactly the
                # "another app owns it" shape.
                assert second.register("Ctrl+Alt+Shift+F24") == "unavailable"
                assert second.registered is False
            finally:
                second.unregister()
        finally:
            first.unregister()
        assert first.status == "disabled"

    def test_a_registration_that_finishes_after_the_wait_is_undone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a loaded machine RegisterHotKey can outlast register()'s wait.
        It used to report "unavailable" while the orphaned thread went on to
        hold the key in a message loop nothing could stop — it fired, and
        blocked every later registration of that key."""
        import ctypes
        import threading
        import time

        import app_core.bridge.hotkey as hotkey_module

        calls: list[str] = []
        finished = threading.Event()

        class SlowUser32:
            def RegisterHotKey(self, *_a: object) -> int:
                time.sleep(0.3)
                calls.append("register")
                return 1

            def UnregisterHotKey(self, *_a: object) -> int:
                calls.append("unregister")
                finished.set()
                return 1

            def GetMessageW(self, *_a: object) -> int:
                calls.append("message-loop")
                finished.set()
                return 0

        class Kernel32:
            def GetCurrentThreadId(self) -> int:
                return 4242

        fake = type("WinDll", (), {"user32": SlowUser32(), "kernel32": Kernel32()})()
        monkeypatch.setattr(ctypes, "windll", fake, raising=False)
        monkeypatch.setattr(hotkey_module, "REGISTER_TIMEOUT_S", 0.05)
        manager = HotkeyManager(lambda: None)
        assert manager.register("Ctrl+Alt+Shift+F23") == "unavailable"
        assert finished.wait(3.0)
        assert calls == ["register", "unregister"], calls
        assert manager.registered is False

    def test_f_key_parsing_rejects_non_ascii_numerals(self) -> None:
        # str.isdigit() is True for 128 codepoints int() rejects, so "f²"
        # raised ValueError out of a parser documented never to raise; and
        # non-ASCII decimals like "f٢" would otherwise map to a real F-key.
        for bad in ["f\u00b2", "ctrl+f\u00b2", "f\u2460", "f\u0662", "f\u1369"]:
            assert parse_accelerator(bad) is None, bad
        assert parse_accelerator("f2") is not None
