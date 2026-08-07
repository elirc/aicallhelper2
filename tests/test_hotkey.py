"""Accelerator parsing (pure — no OS registration in tests)."""

from __future__ import annotations

from app_core.bridge.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
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
