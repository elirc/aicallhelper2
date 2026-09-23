"""Settings validation, per-field fallback, atomicity, and secret semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app_core.errors import AppError
from app_core.llm.base import ProviderRegistry
from app_core.store.settings import SettingsStore
from tests.conftest import FakeProvider
from tests.test_secrets import BrokenKeystore, XorKeystore


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


class TestTransientRenameFailures:
    """Antivirus scanners and the search indexer hold freshly written files
    open for a few milliseconds on Windows; an os.replace landing in that
    window raises PermissionError with nothing actually wrong."""

    def test_a_transient_sharing_violation_does_not_fail_the_save(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        import app_core.store.settings as settings_module

        monkeypatch.setattr(settings_module, "REPLACE_RETRY_S", 0.001)
        real_replace = os.replace
        calls: list[int] = []

        def flaky_replace(src: object, dst: object) -> None:
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(5, "Access is denied")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(settings_module.os, "replace", flaky_replace)
        store = make_store(store_path)
        view = store.patch({"resume": "survived"})
        assert view["resume"] == "survived"
        assert len(calls) == 3
        assert make_store(store_path).view()["resume"] == "survived"
        assert not list(store_path.parent.glob("*.tmp")), "tmp file left behind"

    def test_a_persistent_failure_still_raises_and_leaves_memory_matching_disk(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app_core.store.settings as settings_module

        monkeypatch.setattr(settings_module, "REPLACE_RETRY_S", 0.001)
        store = make_store(store_path)
        store.patch({"resume": "before"})

        def locked_replace(src: object, dst: object) -> None:
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(settings_module.os, "replace", locked_replace)
        with pytest.raises(PermissionError):
            store.patch({"resume": "after"})
        assert store.view()["resume"] == "before"
        assert make_store(store_path).view()["resume"] == "before"
        assert not list(store_path.parent.glob("*.tmp")), "tmp file left behind"


# ------------------------------------------------------------- profiles


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


class TestProfilesMigration:
    def test_legacy_file_migrates_into_one_default_profile_losslessly(
        self, store_path: Path
    ) -> None:
        resume = "  line1\n\n  * bullet\nline3  "
        write_json(store_path, {"resume": resume, "jobDescription": "SRE", "hotkey": "F9"})
        store = make_store(store_path)
        view = store.view()
        assert view["activeProfileId"] == "default"
        assert [p["name"] for p in view["profiles"]] == ["Default"]
        assert view["profiles"][0]["resume"] == resume  # verbatim, never trimmed
        assert view["resume"] == resume and view["jobDescription"] == "SRE"
        assert view["callType"] == "behavioral" and view["focus"] == "" and view["notes"] == ""
        assert view["hotkey"] == "F9"

    def test_load_never_writes_the_file(self, store_path: Path) -> None:
        write_json(store_path, {"resume": "r"})
        before = store_path.read_bytes()
        make_store(store_path)
        assert store_path.read_bytes() == before

    def test_migrated_shape_lands_on_the_next_save_with_mirrors(self, store_path: Path) -> None:
        write_json(store_path, {"resume": "r", "jobDescription": "j"})
        store = make_store(store_path)
        store.patch({"hotkey": "F8"})
        on_disk = json.loads(store_path.read_text(encoding="utf-8"))
        assert on_disk["activeProfileId"] == "default"
        assert on_disk["profiles"][0]["resume"] == "r"
        assert on_disk["resume"] == "r" and on_disk["jobDescription"] == "j"  # mirrors

    def test_profiles_win_over_stale_legacy_mirrors(self, store_path: Path) -> None:
        write_json(
            store_path,
            {
                "resume": "OLD MIRROR",
                "profiles": [{"id": "a", "name": "A", "resume": "from profile"}],
                "activeProfileId": "a",
            },
        )
        assert make_store(store_path).view()["resume"] == "from profile"


class TestProfilesLoadFallback:
    def test_profiles_not_a_list_falls_back_to_legacy_fields(self, store_path: Path) -> None:
        write_json(store_path, {"resume": "legacy", "profiles": {"id": "x"}})
        view = make_store(store_path).view()
        assert view["resume"] == "legacy" and view["activeProfileId"] == "default"

    def test_corrupt_entry_dropped_others_kept_and_fields_fall_back_alone(
        self, store_path: Path
    ) -> None:
        write_json(
            store_path,
            {
                "profiles": [
                    "junk",
                    {"id": "ok1", "name": "Keep", "callType": "nope", "resume": "R", "focus": 5},
                    {"id": "bad id!", "name": "", "notes": "N"},
                ],
                "activeProfileId": "ok1",
            },
        )
        view = make_store(store_path).view()
        assert [p["name"] for p in view["profiles"]] == ["Keep", "Profile 3"]
        keep = view["profiles"][0]
        assert keep["callType"] == "behavioral" and keep["resume"] == "R" and keep["focus"] == ""
        other = view["profiles"][1]
        assert other["notes"] == "N"
        assert other["id"] != "bad id!" and len(other["id"]) == 12  # fresh id, not p3

    def test_unknown_active_id_falls_back_to_the_first_profile(self, store_path: Path) -> None:
        write_json(
            store_path,
            {
                "profiles": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
                "activeProfileId": "zzz",
            },
        )
        assert make_store(store_path).view()["activeProfileId"] == "a"

    def test_duplicate_ids_first_wins(self, store_path: Path) -> None:
        write_json(
            store_path,
            {"profiles": [{"id": "a", "name": "First"}, {"id": "a", "name": "Second"}]},
        )
        assert [p["name"] for p in make_store(store_path).view()["profiles"]] == ["First"]

    def test_more_than_the_cap_is_truncated(self, store_path: Path) -> None:
        write_json(
            store_path, {"profiles": [{"id": f"p{i}", "name": f"P{i}"} for i in range(30)]}
        )
        assert len(make_store(store_path).view()["profiles"]) == 20

    def test_layout_and_font_fields_fall_back_individually(self, store_path: Path) -> None:
        write_json(
            store_path,
            {"layoutMode": "sideways", "prompterFontPx": True, "answerFontPx": 99, "resume": "r"},
        )
        view = make_store(store_path).view()
        assert view["layoutMode"] == "full"
        assert view["prompterFontPx"] == 18 and view["answerFontPx"] == 14
        assert view["resume"] == "r"


class TestProfilesPatch:
    def test_top_level_resume_patch_edits_only_the_active_profile(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch(
            {
                "profiles": [
                    {"id": "a", "name": "A", "resume": "ra"},
                    {"id": "b", "name": "B", "resume": "rb"},
                ],
                "activeProfileId": "b",
            }
        )
        view = store.patch({"resume": "rb2"})
        assert [p["resume"] for p in view["profiles"]] == ["ra", "rb2"]
        assert view["resume"] == "rb2"

    def test_call_type_focus_notes_are_validated_and_visible(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"callType": "sales", "focus": "pricing", "notes": "n"})
        assert (view["callType"], view["focus"], view["notes"]) == ("sales", "pricing", "n")
        with pytest.raises(AppError):
            store.patch({"callType": "poetry"})
        with pytest.raises(AppError):
            store.patch({"focus": "x" * 2001})
        with pytest.raises(AppError):
            store.patch({"notes": 3})

    def test_profiles_replace_assigns_ids_to_null_ids_and_keeps_given_ids(
        self, store_path: Path
    ) -> None:
        store = make_store(store_path)
        view = store.patch(
            {"profiles": [{"id": None, "name": "New"}, {"id": "given", "name": "Given"}]}
        )
        ids = [p["id"] for p in view["profiles"]]
        assert ids[1] == "given" and len(ids[0]) == 12
        assert view["activeProfileId"] == ids[0], "old active id vanished -> first profile"

    def test_profiles_replace_rejections(self, store_path: Path) -> None:
        store = make_store(store_path)
        for bad in (
            [],
            "nope",
            [{"id": "a", "name": ""}],
            [{"id": "a", "name": "A", "callType": "x"}],
            [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}],
            [{"id": "bad id!", "name": "A"}],
            [{"id": f"p{i}", "name": "A"} for i in range(21)],
            [{"id": "a", "name": "A", "focus": "f" * 2001}],
        ):
            with pytest.raises(AppError):
                store.patch({"profiles": bad})
        assert store.view()["profiles"][0]["name"] == "Default", "nothing landed"

    def test_active_profile_id_must_exist(self, store_path: Path) -> None:
        with pytest.raises(AppError):
            make_store(store_path).patch({"activeProfileId": "ghost"})

    def test_new_profile_can_be_activated_in_the_same_patch(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch(
            {
                "profiles": [{"id": "default", "name": "Default"}, {"id": "p1", "name": "Sales"}],
                "activeProfileId": "p1",
            }
        )
        assert view["activeProfileId"] == "p1"

    def test_view_lists_call_types_in_order(self, store_path: Path) -> None:
        view = make_store(store_path).view()
        assert [c["id"] for c in view["callTypes"]][:2] == ["behavioral", "technical"]

    def test_answer_config_carries_the_active_profile(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch({"callType": "technical", "focus": "Rust", "notes": "n", "resume": "r"})
        cfg = store.answer_config()
        assert (cfg.call_type, cfg.focus, cfg.notes, cfg.resume) == ("technical", "Rust", "n", "r")

    def test_mirror_keys_track_the_active_profile_on_switch(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.patch(
            {
                "profiles": [
                    {"id": "a", "name": "A", "resume": "ra"},
                    {"id": "b", "name": "B", "resume": "rb"},
                ],
                "activeProfileId": "a",
            }
        )
        store.patch({"activeProfileId": "b"})
        on_disk = json.loads(store_path.read_text(encoding="utf-8"))
        assert on_disk["resume"] == "rb"

    def test_layout_mode_and_font_sizes_validate(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"layoutMode": "prompter", "prompterFontPx": 28, "answerFontPx": 12})
        assert view["layoutMode"] == "prompter"
        assert store.layout_mode() == "prompter"
        assert (view["prompterFontPx"], view["answerFontPx"]) == (28, 12)
        for bad in ({"layoutMode": "wide"}, {"prompterFontPx": 13}, {"prompterFontPx": "18"},
                    {"prompterFontPx": True}, {"answerFontPx": 23}):
            with pytest.raises(AppError):
                store.patch(bad)

    def test_per_mode_window_bounds_are_independent(self, store_path: Path) -> None:
        store = make_store(store_path)
        store.set_window_bounds({"x": 1, "y": 2, "width": 460, "height": 700})
        store.set_window_bounds({"x": 9, "y": 8, "width": 720, "height": 260}, mode="prompter")
        assert store.window_bounds()["x"] == 1  # type: ignore[index]
        assert store.window_bounds(mode="prompter")["x"] == 9  # type: ignore[index]
        reloaded = make_store(store_path)
        assert reloaded.window_bounds(mode="prompter")["width"] == 720  # type: ignore[index]
        assert reloaded.window_bounds()["width"] == 460  # type: ignore[index]


# ------------------------------------------------- R02: encryption failures


def plain(value: str) -> str:
    import base64

    return "plain:" + base64.b64encode(value.encode()).decode()


class TestEncryptionFailsClosed:
    """R02: a DPAPI failure used to store the key as decodable plaintext
    while Settings promised encryption. Now the save fails visibly and
    nothing it carried is applied."""

    def test_a_failed_encryption_refuses_the_save_and_keeps_the_old_key(
        self, store_path: Path
    ) -> None:
        store = make_store(store_path)
        store.patch({"keys": {"deepgram": "dg-old"}, "resume": "before"})
        before = store_path.read_bytes()
        store._keystore = BrokenKeystore()  # DPAPI starts failing
        with pytest.raises(AppError) as info:
            store.patch({"keys": {"deepgram": "dg-new"}, "resume": "after"})
        assert "NOT saved" in info.value.message
        assert store_path.read_bytes() == before, "the file changed"
        store._keystore = XorKeystore()
        assert store.get_secret("deepgram") == "dg-old"
        assert store.view()["resume"] == "before", "part of a refused save landed"
        assert "plain:" not in store_path.read_text(encoding="utf-8")

    def test_saved_keys_report_how_they_are_stored(self, store_path: Path) -> None:
        store = make_store(store_path)
        view = store.patch({"keys": {"deepgram": "dg"}})
        assert view["keyStorage"] == {"deepgram": "encrypted"}

    def test_a_legacy_plaintext_key_is_reported_and_migrated_on_save(
        self, store_path: Path
    ) -> None:
        write_json(store_path, {"secrets": {"deepgram": plain("dg-legacy")}})
        store = make_store(store_path)
        assert store.get_secret("deepgram") == "dg-legacy"  # never lost on upgrade
        assert store.view()["keyStorage"] == {"deepgram": "plaintext"}
        view = store.patch({"answerStyle": "brief"})
        assert view["keyStorage"] == {"deepgram": "encrypted"}
        assert "plain:" not in store_path.read_text(encoding="utf-8")
        assert make_store(store_path).get_secret("deepgram") == "dg-legacy"

    def test_a_legacy_key_that_cannot_be_reencrypted_keeps_value_and_true_status(
        self, store_path: Path
    ) -> None:
        write_json(store_path, {"secrets": {"deepgram": plain("dg-legacy")}})
        store = SettingsStore(store_path, BrokenKeystore(), make_registry())
        view = store.patch({"answerStyle": "brief"})  # unrelated save succeeds
        assert view["answerStyle"] == "brief"
        assert view["keyStorage"] == {"deepgram": "plaintext"}, "migration implied"
        assert store.get_secret("deepgram") == "dg-legacy"


# --------------------------------------- R05: preserve an unloadable file


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".*.bak"))


class TestUnloadableFilePreservation:
    """R05: a corrupt or unreadable file loads as defaults, and the first
    automatic geometry save used to overwrite it. Launching and closing the
    window was enough to lose the profiles and keys in it."""

    def test_a_geometry_save_backs_up_a_corrupt_file_before_replacing_it(
        self, store_path: Path
    ) -> None:
        original = b'{"resume": "precious", broken'
        store_path.write_bytes(original)
        store = make_store(store_path)
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        saved = backups(store_path)
        assert len(saved) == 1 and saved[0].read_bytes() == original
        assert ".invalid-" in saved[0].name
        assert json.loads(store_path.read_text(encoding="utf-8"))["windowBounds"]["x"] == 1
        assert store.view()["settingsFile"] == {"load": "invalid", "backup": saved[0].name}
        # Exactly one backup, however many writes follow.
        store.set_window_bounds({"x": 3, "y": 4, "width": 500, "height": 600})
        store.patch({"resume": "new"})
        assert len(backups(store_path)) == 1

    def test_if_the_backup_cannot_be_made_nothing_is_overwritten(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app_core.store.settings as settings_module

        original = b"\xff\xfe not utf-8 at all"
        store_path.write_bytes(original)
        store = make_store(store_path)

        def no_backup(*_a: object, **_k: object) -> object:
            raise OSError("disk full")

        monkeypatch.setattr(settings_module, "open", no_backup, raising=False)
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})  # no raise
        with pytest.raises(AppError) as info:
            store.patch({"resume": "x"})
        assert "NOT overwritten" in info.value.message
        assert store_path.read_bytes() == original
        assert backups(store_path) == []
        assert store.view()["settingsFile"]["backup"] is None
        monkeypatch.undo()
        store.patch({"resume": "x"})  # preservation now succeeds, then the write
        assert [b.read_bytes() for b in backups(store_path)] == [original]

    def test_an_unreadable_file_is_distinguished_and_preserved(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(store_path, {"resume": "valid but locked at startup"})
        original = store_path.read_bytes()
        real_read = Path.read_bytes
        calls: list[int] = []

        def locked_once(self: Path) -> bytes:
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(32, "being used by another process")
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", locked_once)
        store = make_store(store_path)
        assert store.view()["settingsFile"]["load"] == "unreadable"
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        saved = backups(store_path)
        assert len(saved) == 1 and ".unreadable-" in saved[0].name
        assert real_read(saved[0]) == original

    def test_a_file_still_unreadable_at_write_time_is_not_overwritten(
        self, store_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(store_path, {"resume": "locked"})
        original = store_path.read_bytes()

        def always_locked(self: Path) -> bytes:
            raise PermissionError(32, "being used by another process")

        monkeypatch.setattr(Path, "read_bytes", always_locked)
        store = make_store(store_path)
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        monkeypatch.undo()
        assert store_path.read_bytes() == original

    def test_a_missing_file_is_a_first_run_with_no_backup(self, store_path: Path) -> None:
        store = make_store(store_path)
        assert store.view()["settingsFile"] == {"load": "missing", "backup": None}
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        assert backups(store_path) == []

    def test_a_valid_file_reports_ok_and_is_never_backed_up(self, store_path: Path) -> None:
        write_json(store_path, {"resume": "r"})
        store = make_store(store_path)
        store.patch({"resume": "r2"})
        assert store.view()["settingsFile"] == {"load": "ok", "backup": None}
        assert backups(store_path) == []

    def test_backup_names_never_collide(self, store_path: Path) -> None:
        store_path.write_text("[not an object]", encoding="utf-8")
        first = make_store(store_path)
        second = make_store(store_path)  # e.g. two launches, same second
        first.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        store_path.write_text("[not an object]", encoding="utf-8")
        second.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        assert len(backups(store_path)) == 2


# ------------------------------------------------ R09: save ordering


class TestSaveRevision:
    """R09: the lock and atomic replace prevent torn files, not stale writes.
    Two bridge threads can apply saves in the reverse of submission order;
    a save built on an older view must not overwrite a newer one."""

    def test_each_save_advances_the_revision(self, store_path: Path) -> None:
        store = make_store(store_path)
        first = store.view()["settingsRevision"]
        assert store.patch({"resume": "a"})["settingsRevision"] == first + 1
        store.set_window_bounds({"x": 1, "y": 2, "width": 500, "height": 600})
        assert store.view()["settingsRevision"] == first + 1, "geometry is not a save"

    def test_a_stale_whole_profile_save_cannot_overwrite_a_newer_one(
        self, store_path: Path
    ) -> None:
        store = make_store(store_path)
        base = store.view()["settingsRevision"]
        newer = [{"id": "default", "name": "Default", "resume": "version B"}]
        older = [{"id": "default", "name": "Default", "resume": "version A"}]
        store.patch({"profiles": newer, "baseRevision": base})
        with pytest.raises(AppError) as info:
            store.patch({"profiles": older, "baseRevision": base})  # arrived late
        assert "newer save" in info.value.message
        assert store.view()["resume"] == "version B"
        assert make_store(store_path).view()["resume"] == "version B"

    def test_a_malformed_base_revision_is_rejected(self, store_path: Path) -> None:
        store = make_store(store_path)
        for bad in ("0", True, None, 1.0):
            with pytest.raises(AppError):
                store.patch({"resume": "x", "baseRevision": bad})
        assert store.view()["resume"] == ""

    def test_saves_without_a_base_revision_keep_last_write_wins(
        self, store_path: Path
    ) -> None:
        store = make_store(store_path)
        store.patch({"prompterFontPx": 20})
        assert store.patch({"prompterFontPx": 22})["prompterFontPx"] == 22
