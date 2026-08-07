"""Settings validation, per-field fallback, atomicity, and secret semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_core.errors import AppError
from app_core.llm.base import ProviderRegistry
from app_core.store.settings import SettingsStore
from tests.conftest import FakeProvider
from tests.test_secrets import XorKeystore


def make_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    fake = FakeProvider()
    registry.register(fake)

    class Anthropic(FakeProvider):
        id = "anthropic"
        display_name = "Claude Haiku 4.5 (recommended)"

    registry.register(Anthropic())
    return registry


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def make_store(path: Path) -> SettingsStore:
    return SettingsStore(path, XorKeystore(), make_registry())


class TestLoad:
    def test_missing_file_loads_defaults(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.view()
        assert view["resume"] == ""
        assert view["llmProvider"] == "anthropic"
        assert view["answerStyle"] == "balanced"
        assert view["hotkey"] == "Ctrl+Shift+Space"
        assert view["alwaysOnTop"] is True

    def test_unparseable_file_loads_defaults_never_crashes(self, store_path: Path) -> None:
        store_path.write_text("{not json", encoding="utf-8")
        assert make_store(store_path).view()["answerStyle"] == "balanced"

    def test_non_object_file_loads_defaults(self, store_path: Path) -> None:
        store_path.write_text('["array"]', encoding="utf-8")
        assert make_store(store_path).view()["answerStyle"] == "balanced"

    def test_per_field_fallback_one_corrupt_value_costs_nothing_else(
        self, store_path: Path
    ) -> None:
        store_path.write_text(
            json.dumps(
                {
                    "resume": "my precious resume",
                    "jobDescription": 42,  # corrupt
                    "alwaysOnTop": "yes",  # corrupt
                    "llmProvider": "unknown-provider",  # corrupt
                    "answerStyle": "brief",
                    "hotkey": "Ctrl+Alt+R",
                }
            ),
            encoding="utf-8",
        )
        view = make_store(store_path).view()
        assert view["resume"] == "my precious resume"  # survived
        assert view["jobDescription"] == ""
        assert view["alwaysOnTop"] is True
        assert view["llmProvider"] == "anthropic"
        assert view["answerStyle"] == "brief"  # survived
        assert view["hotkey"] == "Ctrl+Alt+R"  # survived

    def test_oversize_profile_field_falls_back(self, store_path: Path) -> None:
        store_path.write_text(
            json.dumps({"resume": "x" * 200_001}), encoding="utf-8"
        )
        assert make_store(store_path).view()["resume"] == ""

    def test_empty_hotkey_means_disabled_not_default(self, store_path: Path) -> None:
        store_path.write_text(json.dumps({"hotkey": ""}), encoding="utf-8")
        assert make_store(store_path).view()["hotkey"] == ""

    def test_corrupt_secrets_dict_falls_back_alone(self, store_path: Path) -> None:
        store_path.write_text(
            json.dumps({"secrets": "not-a-dict", "resume": "keep me"}), encoding="utf-8"
        )
        store = make_store(store_path)
        assert store.get_secret("deepgram") is None
        assert store.view()["resume"] == "keep me"


class TestPatchValidation:
    def test_patch_returns_fresh_view(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"answerStyle": "detailed"})
        assert view["answerStyle"] == "detailed"

    def test_invalid_style_rejected(self, store_path: Path) -> None:
        store = make_store(store_path)
        with pytest.raises(AppError):
            store.patch({"answerStyle": "verbose"})

    def test_invalid_provider_rejected(self, store_path: Path) -> None:
        with pytest.raises(AppError):
            make_store(store_path).patch({"llmProvider": "openai"})

    def test_oversize_resume_rejected(self, store_path: Path) -> None:
        with pytest.raises(AppError):
            make_store(store_path).patch({"resume": "x" * 200_001})

    def test_resume_stored_verbatim_not_trimmed(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"resume": "  leading and trailing  \n"})
        assert view["resume"] == "  leading and trailing  \n"

    def test_hotkey_trimmed_on_save(self, store_path: Path) -> None:
        store = make_store(store_path)
        assert store.patch({"hotkey": "  Ctrl+R  "})["hotkey"] == "Ctrl+R"
        # Whitespace-only -> "" = disabled (raw spaces would make
        # registration throw), and it must NOT spring back to the default.
        assert store.patch({"hotkey": "   "})["hotkey"] == ""

    def test_unknown_patch_fields_ignored(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"unknownField": 123})
        assert "unknownField" not in view


class TestSecretSemantics:
    def test_view_exposes_booleans_never_key_material(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "dg-secret-key"}})
        view = store.patch({})
        assert view["hasDeepgramKey"] is True
        assert view["hasAnthropicKey"] is False
        assert view["hasFakeKey"] is False
        assert "dg-secret-key" not in json.dumps(view)

    def test_has_key_fields_generated_from_registry(self, store_path: Path) -> None:
        view = make_store(store_path).view()
        assert {"hasDeepgramKey", "hasFakeKey", "hasAnthropicKey"} <= set(view)

    def test_omitted_key_field_leaves_key_untouched(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "dg-1"}})
        store.patch({"resume": "new resume"})  # no keys at all
        assert store.get_secret("deepgram") == "dg-1"
        store.patch({"keys": {"anthropic": "an-1"}})  # other provider only
        assert store.get_secret("deepgram") == "dg-1"

    def test_empty_or_whitespace_key_clears(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "dg-1"}})
        store.patch({"keys": {"deepgram": "   "}})
        assert store.get_secret("deepgram") is None
        assert store.view()["hasDeepgramKey"] is False

    def test_key_trimmed_before_store(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "  dg-1  "}})
        assert store.get_secret("deepgram") == "dg-1"

    def test_keys_encrypted_on_disk(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "dg-secret"}})
        on_disk = store_path.read_text(encoding="utf-8")
        assert "dg-secret" not in on_disk
        assert "enc:" in on_disk

    def test_undecryptable_secret_reads_as_unset_in_view(self, store_path: Path) -> None:
        store_path.write_text(
            json.dumps({"secrets": {"deepgram": "enc:AAAA"}}), encoding="utf-8"
        )
        store = make_store(store_path)
        assert store.get_secret("deepgram") is None
        assert store.view()["hasDeepgramKey"] is False


class TestAtomicity:
    def test_write_goes_through_tmp_and_replace(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"resume": "hello"})
        assert store_path.exists()
        assert not Path(str(store_path) + ".tmp").exists()
        assert json.loads(store_path.read_text(encoding="utf-8"))["resume"] == "hello"

    def test_failed_write_leaves_memory_matching_disk(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(store_path)
        store.patch({"resume": "committed"})

        import app_core.store.settings as settings_module

        def boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(settings_module.os, "replace", boom)
        with pytest.raises(OSError):
            store.patch({"resume": "never lands"})
        monkeypatch.undo()
        # In-memory cache still matches disk: the failed patch left no trace.
        assert store.view()["resume"] == "committed"
        assert json.loads(store_path.read_text(encoding="utf-8"))["resume"] == "committed"

    def test_window_bounds_save_never_raises(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(store_path)
        import app_core.store.settings as settings_module

        monkeypatch.setattr(
            settings_module.os, "replace", lambda *a: (_ for _ in ()).throw(OSError())
        )
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})  # no raise

    def test_window_bounds_roundtrip(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.set_window_bounds({"x": 10, "y": 20, "width": 500, "height": 600})
        reloaded = make_store(store_path)
        assert reloaded.window_bounds() == {"x": 10, "y": 20, "width": 500, "height": 600}


class TestConcurrentWriters:
    def test_a_bounds_save_racing_a_patch_never_loses_either_writer(
        self, store_path: Path
    ) -> None:
        """Realistic trigger: the user drags the window (debounced bounds
        save on a timer thread) and clicks Save within the same half second.
        Unsynchronized read-modify-write dropped one writer's fields from BOTH
        disk and the in-memory cache."""
        import threading

        store = make_store(store_path)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2, timeout=10)

        def save_keys() -> None:
            try:
                barrier.wait()
                for i in range(40):
                    store.patch({"keys": {"deepgram": f"dg-{i}"}, "resume": f"r{i}"})
            except BaseException as exc:
                errors.append(exc)

        def save_bounds() -> None:
            try:
                barrier.wait()
                for i in range(40):
                    store.set_window_bounds({"x": i, "y": i, "width": 500, "height": 600})
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=save_keys),
            threading.Thread(target=save_bounds),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        # Both writers' last values survived, in memory AND on disk.
        assert store.get_secret("deepgram") == "dg-39"
        assert store.view()["resume"] == "r39"
        assert store.window_bounds() is not None
        reloaded = make_store(store_path)
        assert reloaded.get_secret("deepgram") == "dg-39"
        assert reloaded.view()["resume"] == "r39"
        assert reloaded.window_bounds() is not None

    def test_no_stray_tmp_files_are_left_behind(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"resume": "x"})
        assert list(store_path.parent.glob("*.tmp")) == []


class TestKeyCharsetValidation:
    def test_non_ascii_key_is_refused_with_an_actionable_message(
        self, store_path: Path
    ) -> None:
        # Smart quotes / hidden characters from copy-paste cannot go in an
        # HTTP header: httpx raises UnicodeEncodeError at send time, which
        # would surface as a useless "internal error" mid-answer.
        store = make_store(store_path)
        with pytest.raises(AppError) as info:
            store.patch({"keys": {"deepgram": "sk-café"}})
        assert "ASCII" in info.value.message
        assert store.get_secret("deepgram") is None

    def test_ascii_keys_with_punctuation_still_accepted(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "sk-ant_api03-AbC.123-_xyz"}})
        assert store.get_secret("deepgram") == "sk-ant_api03-AbC.123-_xyz"
