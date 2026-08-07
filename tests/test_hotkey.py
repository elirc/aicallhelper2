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
