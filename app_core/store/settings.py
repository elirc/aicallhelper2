"""The settings file: validation, atomic writes, write-only secrets, profiles.

The file is user-writable and survives upgrades — untrusted input. Every
field falls back to its default INDIVIDUALLY: one corrupt value must never
cost the user their resume or keys. All I/O here is synchronous; async
callers wrap calls in `asyncio.to_thread` (file writes and DPAPI must never
run on the event loop).

Profiles: the resume, job description, call type, focus and notes live in
`profiles[]`, one entry per opportunity or kind of call, with
`activeProfileId` naming the one the answer pipeline uses. The legacy
top-level `resume` / `jobDescription` keys are kept as WRITE-ONLY mirrors of
the active profile (an older build that opens the file keeps working with
the active profile), and a file with no valid `profiles` list is migrated
in memory into one "Default" profile built from those legacy keys — load
never writes, so a bad launch cannot damage the file.

Reads that pair `activeProfileId` with `profiles` snapshot `self._data`
ONCE: `_save` swaps the whole dict, and a field-by-field read racing it
could pair an id with a list that lacks it.

A file that exists but could not be read ("unreadable") or parsed
("invalid") still loads as defaults, but it is PRESERVED before the first
write of any kind — including the automatic window-geometry save — as a
uniquely named `.bak` copy next to it. If that copy cannot be made the write
is refused: nothing overwrites bytes that could not be preserved. A missing
file is a first run and needs no backup. `view()` reports which case
happened (`settingsFile`) so the UI can tell the user where the copy is.

Saves carry an optional `baseRevision` precondition (the `settingsRevision`
the page last adopted); a save built on an older view is rejected instead of
overwriting a newer one, whatever order the bridge threads ran in.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from app_core.contracts import DEEPGRAM_SECRET_ID, AnswerConfig
from app_core.errors import AppError
from app_core.llm.base import ProviderRegistry
from app_core.llm.prompt import CALL_TYPES, DEFAULT_CALL_TYPE, call_type_choices
from app_core.store.secrets import (
    PLAIN_PREFIX,
    Keystore,
    SecretEncryptionError,
    decode_secret,
    encode_secret,
    secret_storage,
)

LoadStatus = Literal["ok", "missing", "unreadable", "invalid"]

MAX_PROFILE_CHARS = 200_000
MAX_HOTKEY_CHARS = 100
DEFAULT_HOTKEY = "Ctrl+Shift+Space"
ANSWER_STYLES = ("brief", "balanced", "detailed")
LAYOUT_MODES = ("full", "prompter")
PROMPTER_FONT_RANGE = (14, 28)
DEFAULT_PROMPTER_FONT_PX = 18
ANSWER_FONT_RANGE = (12, 22)
DEFAULT_ANSWER_FONT_PX = 14

MAX_PROFILES = 20
MAX_PROFILE_NAME_CHARS = 60
MAX_FOCUS_CHARS = 2_000
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "Default"

# Which settings key holds the window geometry for each layout mode.
_BOUNDS_KEYS = {"full": "windowBounds", "prompter": "prompterBounds"}


def _new_profile_id() -> str:
    return uuid.uuid4().hex[:12]


def _blank_profile(profile_id: str, name: str) -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": name,
        "callType": DEFAULT_CALL_TYPE,
        "focus": "",
        "resume": "",
        "jobDescription": "",
        "notes": "",
    }


class SettingsStore:
    def __init__(
        self, path: Path, keystore: Keystore | None, registry: ProviderRegistry
    ) -> None:
        self._path = path
        self._keystore = keystore
        self._registry = registry
        # Writers run on different threads (patch via asyncio.to_thread from
        # the bridge; window bounds from a debounce timer and the close
        # handler). Without this lock the read-modify-write of the whole
        # settings dict interleaves and one writer's fields — a just-saved API
        # key, or the resume — vanish from BOTH disk and the cache.
        self._lock = threading.RLock()
        self._load_status: LoadStatus = "ok"
        self._backup_path: Path | None = None
        # Bumped by every successful patch; the page echoes it back as
        # `baseRevision` so a stale save cannot overwrite a newer one.
        self._revision = 0
        self._data = self._load()

    # -------------------------------------------------------------- loading

    def _defaults(self) -> dict[str, Any]:
        provider_ids = self._registry.ids()
        default_provider = "anthropic" if "anthropic" in provider_ids else (
            provider_ids[0] if provider_ids else "anthropic"
        )
        return {
            "profiles": [_blank_profile(DEFAULT_PROFILE_ID, DEFAULT_PROFILE_NAME)],
            "activeProfileId": DEFAULT_PROFILE_ID,
            "resume": "",
            "jobDescription": "",
            "alwaysOnTop": True,
            "llmProvider": default_provider,
            "answerStyle": "balanced",
            "hotkey": DEFAULT_HOTKEY,
            "layoutMode": "full",
            "prompterFontPx": DEFAULT_PROMPTER_FONT_PX,
            "answerFontPx": DEFAULT_ANSWER_FONT_PX,
            "secrets": {},
            "windowBounds": None,
            "prompterBounds": None,
        }

    def _load(self) -> dict[str, Any]:
        defaults = self._defaults()
        # Missing, unreadable or unparseable files all load as defaults, never
        # a crash — but they are told apart, because only the latter two hold
        # bytes that must be preserved before the first write (_save).
        try:
            text = self._path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            self._load_status = "missing"
            return defaults
        except UnicodeDecodeError:
            self._load_status = "invalid"
            return defaults
        except OSError:
            self._load_status = "unreadable"
            return defaults
        try:
            raw = json.loads(text)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            self._load_status = "invalid"
            return defaults
        data = dict(defaults)
        # Legacy top-level profile text, read with the original rules. When a
        # valid profiles list exists these are mirrors and are ignored.
        legacy_resume = _load_profile_text(raw.get("resume"))
        legacy_jd = _load_profile_text(raw.get("jobDescription"))
        profiles = _load_profiles(raw.get("profiles"))
        if not profiles:
            # Migration: one Default profile carrying the legacy text, lossless
            # because the legacy fields were read with the original code path.
            migrated = _blank_profile(DEFAULT_PROFILE_ID, DEFAULT_PROFILE_NAME)
            migrated["resume"] = legacy_resume
            migrated["jobDescription"] = legacy_jd
            profiles = [migrated]
        data["profiles"] = profiles
        active = raw.get("activeProfileId")
        ids = {p["id"] for p in profiles}
        data["activeProfileId"] = (
            active if isinstance(active, str) and active in ids else profiles[0]["id"]
        )
        aot = raw.get("alwaysOnTop")
        if isinstance(aot, bool):
            data["alwaysOnTop"] = aot
        provider = raw.get("llmProvider")
        if isinstance(provider, str) and provider in self._registry.ids():
            data["llmProvider"] = provider
        style = raw.get("answerStyle")
        if isinstance(style, str) and style in ANSWER_STYLES:
            data["answerStyle"] = style
        hotkey = raw.get("hotkey")
        if isinstance(hotkey, str) and len(hotkey) <= MAX_HOTKEY_CHARS:
            # Empty string means "shortcut disabled" and must NOT spring back
            # to the default.
            data["hotkey"] = hotkey.strip()
        mode = raw.get("layoutMode")
        if isinstance(mode, str) and mode in LAYOUT_MODES:
            data["layoutMode"] = mode
        font = raw.get("prompterFontPx")
        if _is_font_px(font, PROMPTER_FONT_RANGE):
            data["prompterFontPx"] = font
        answer_font = raw.get("answerFontPx")
        if _is_font_px(answer_font, ANSWER_FONT_RANGE):
            data["answerFontPx"] = answer_font
        secrets = raw.get("secrets")
        if isinstance(secrets, dict):
            data["secrets"] = {
                k: v for k, v in secrets.items() if isinstance(k, str) and isinstance(v, str)
            }
        for key in _BOUNDS_KEYS.values():
            bounds = raw.get(key)
            if isinstance(bounds, dict):
                data[key] = bounds  # sanitized at restore time (bounds.py)
        _sync_mirrors(data)
        return data

    # -------------------------------------------------------------- writing

    def _save(self, data: dict[str, Any]) -> None:
        """Atomic write: tmp file then os.replace. A crash or full disk
        mid-write must not truncate the file into "defaults". The in-memory
        cache updates only AFTER the write lands, so a failed write leaves
        memory matching disk."""
        with self._lock:
            self._preserve_unloaded_file()
            # Per-writer tmp name: two concurrent writers sharing one tmp path
            # can interleave bytes before either os.replace lands.
            tmp = Path(f"{self._path}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                _replace_with_retry(tmp, self._path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            self._data = data

    def _preserve_unloaded_file(self) -> None:
        """Before the first write over a file that exists but was not loaded,
        copy its bytes to a uniquely named backup. Raises (so the caller's
        write is refused) when the bytes cannot be preserved."""
        if self._load_status not in ("unreadable", "invalid") or self._backup_path is not None:
            return
        try:
            original = self._path.read_bytes()
        except FileNotFoundError:
            # Gone since load (the user moved it away): nothing to preserve.
            self._load_status = "missing"
            return
        except OSError as exc:
            raise _preservation_error() from exc
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(
            f"{self._path.name}.{self._load_status}-{stamp}-{uuid.uuid4().hex[:8]}.bak"
        )
        try:
            # "xb": never overwrite anything, including an earlier backup.
            with open(backup, "xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)
            raise _preservation_error() from exc
        self._backup_path = backup

    # ------------------------------------------------------------------ API

    def view(self) -> dict[str, Any]:
        """What the frontend sees. NEVER includes key material — only
        per-provider booleans, generated from the registry so a new provider
        gets its key field and first-run nudge for free. The top-level
        resume/jobDescription/callType/focus/notes are the ACTIVE profile's."""
        data = self._data  # one snapshot; _save swaps the dict atomically
        active = _active_profile(data)
        out: dict[str, Any] = {
            "resume": active["resume"],
            "jobDescription": active["jobDescription"],
            "callType": active["callType"],
            "focus": active["focus"],
            "notes": active["notes"],
            "activeProfileId": active["id"],
            "profiles": [dict(p) for p in data["profiles"]],
            "callTypes": call_type_choices(),
            "alwaysOnTop": data["alwaysOnTop"],
            "llmProvider": data["llmProvider"],
            "answerStyle": data["answerStyle"],
            "hotkey": data["hotkey"],
            "layoutMode": data["layoutMode"],
            "prompterFontPx": data["prompterFontPx"],
            "answerFontPx": data["answerFontPx"],
            "providers": self._registry.choices(),
            "settingsRevision": self._revision,
            "settingsFile": {
                "load": self._load_status,
                "backup": self._backup_path.name if self._backup_path is not None else None,
            },
        }
        # How each SAVED key is protected on disk: "encrypted" (DPAPI) or
        # "plaintext" (a legacy value an older build wrote when DPAPI failed;
        # re-encrypted by the next successful save). Never key material.
        storage: dict[str, str] = {}
        secrets = data["secrets"]
        for secret_id in [DEEPGRAM_SECRET_ID, *self._registry.ids()]:
            present = self._get_secret_from(data, secret_id) is not None
            out[_has_key_field(secret_id)] = present
            kind = secret_storage(secrets.get(secret_id)) if present else None
            if kind is not None:
                storage[secret_id] = kind
        out["keyStorage"] = storage
        return out

    def patch(self, patch: Mapping[str, object]) -> dict[str, Any]:
        """Validated patch; returns the fresh view. Invalid values raise
        AppError (the bridge turns it into an error Result envelope)."""
        with self._lock:
            return self._patch_locked(patch)

    def _patch_locked(self, patch: Mapping[str, object]) -> dict[str, Any]:
        if "baseRevision" in patch:
            base = patch["baseRevision"]
            if isinstance(base, bool) or not isinstance(base, int):
                raise AppError("internal", "baseRevision must be a number.")
            if base != self._revision:
                raise AppError(
                    "internal",
                    "Settings were changed by a newer save, so this older save "
                    "was not applied. Review the current settings and save again.",
                )
        data = dict(self._data)
        if "profiles" in patch:
            data["profiles"] = _validate_profiles(patch["profiles"])
            if data["activeProfileId"] not in {p["id"] for p in data["profiles"]}:
                data["activeProfileId"] = data["profiles"][0]["id"]
        if "activeProfileId" in patch:
            value = patch["activeProfileId"]
            if not isinstance(value, str) or value not in {p["id"] for p in data["profiles"]}:
                raise AppError("internal", "Unknown profile.")
            data["activeProfileId"] = value
        # Active-profile fields, copy-on-write: never mutate the cached list.
        active_patch: dict[str, Any] = {}
        if "resume" in patch:
            active_patch["resume"] = _require_profile_text(patch["resume"], "Resume")
        if "jobDescription" in patch:
            active_patch["jobDescription"] = _require_profile_text(
                patch["jobDescription"], "Job description"
            )
        if "notes" in patch:
            active_patch["notes"] = _require_profile_text(patch["notes"], "Notes")
        if "callType" in patch:
            active_patch["callType"] = _require_call_type(patch["callType"])
        if "focus" in patch:
            active_patch["focus"] = _require_focus(patch["focus"])
        if active_patch:
            data["profiles"] = [
                {**p, **active_patch} if p["id"] == data["activeProfileId"] else p
                for p in data["profiles"]
            ]
        if "alwaysOnTop" in patch:
            value = patch["alwaysOnTop"]
            if not isinstance(value, bool):
                raise AppError("internal", "alwaysOnTop must be true or false.")
            data["alwaysOnTop"] = value
        if "llmProvider" in patch:
            value = patch["llmProvider"]
            if not isinstance(value, str) or value not in self._registry.ids():
                raise AppError("internal", "Unknown answer provider.")
            data["llmProvider"] = value
        if "answerStyle" in patch:
            value = patch["answerStyle"]
            if not isinstance(value, str) or value not in ANSWER_STYLES:
                raise AppError("internal", "Answer style must be brief, balanced or detailed.")
            data["answerStyle"] = value
        if "layoutMode" in patch:
            value = patch["layoutMode"]
            if not isinstance(value, str) or value not in LAYOUT_MODES:
                raise AppError("internal", "Layout must be full or prompter.")
            data["layoutMode"] = value
        if "prompterFontPx" in patch:
            value = patch["prompterFontPx"]
            if not _is_font_px(value, PROMPTER_FONT_RANGE):
                lo, hi = PROMPTER_FONT_RANGE
                raise AppError("internal", f"Prompter text size must be between {lo} and {hi}.")
            data["prompterFontPx"] = value
        if "answerFontPx" in patch:
            value = patch["answerFontPx"]
            if not _is_font_px(value, ANSWER_FONT_RANGE):
                lo, hi = ANSWER_FONT_RANGE
                raise AppError("internal", f"Answer text size must be between {lo} and {hi}.")
            data["answerFontPx"] = value
        if "hotkey" in patch:
            value = patch["hotkey"]
            if not isinstance(value, str):
                raise AppError("internal", "Hotkey must be text.")
            # Trimmed on save; whitespace-only -> "" = disabled (raw spaces
            # would make shortcut registration throw).
            trimmed = value.strip()
            if len(trimmed) > MAX_HOTKEY_CHARS:
                raise AppError("internal", "Hotkey is too long (max 100 characters).")
            data["hotkey"] = trimmed
        if "keys" in patch:
            keys = patch["keys"]
            if not isinstance(keys, dict):
                raise AppError("internal", "keys must be an object of provider -> key.")
            known = {DEEPGRAM_SECRET_ID, *self._registry.ids()}
            secrets = dict(data["secrets"])
            for secret_id, key_value in keys.items():
                if not isinstance(secret_id, str) or secret_id not in known:
                    continue  # unknown provider ids are ignored, not fatal
                if not isinstance(key_value, str):
                    raise AppError("internal", "API keys must be text.")
                trimmed_key = key_value.strip()
                if not trimmed_key.isascii():
                    # Non-ASCII cannot go in an HTTP header: httpx would raise
                    # a UnicodeEncodeError at send time, surfacing as a
                    # useless "internal error" mid-answer. Refuse at the door
                    # with a message that names the real problem.
                    raise AppError(
                        "internal",
                        "API keys must be plain ASCII — check for smart quotes "
                        "or hidden characters from copy-paste.",
                    )
                if not trimmed_key:
                    # An empty (or whitespace-only) key value CLEARS the
                    # stored key; omitting the field leaves it untouched.
                    secrets.pop(secret_id, None)
                else:
                    try:
                        secrets[secret_id] = encode_secret(trimmed_key, self._keystore)
                    except SecretEncryptionError:
                        # Fail CLOSED: nothing in this save is applied and the
                        # previously saved key (if any) stays exactly as is.
                        raise AppError(
                            "internal",
                            "Windows could not encrypt the API key, so it was NOT "
                            "saved and nothing else in this save was applied. Your "
                            "previously saved key is unchanged. Try again; if it "
                            "keeps failing, restart Windows.",
                        ) from None
            data["secrets"] = secrets
        data["secrets"] = self._reencrypt_legacy(data["secrets"])
        _sync_mirrors(data)
        self._save(data)
        self._revision += 1
        return self.view()

    def _reencrypt_legacy(self, secrets: dict[str, str]) -> dict[str, str]:
        """Migrate legacy `plain:` keys to DPAPI on an explicit save. A value
        that cannot be re-encrypted keeps its working plaintext form, and the
        view keeps reporting it as "plaintext" — migration is never implied
        just because some other field saved."""
        if not any(v.startswith(PLAIN_PREFIX) for v in secrets.values()):
            return secrets
        migrated = dict(secrets)
        for secret_id, stored in secrets.items():
            if not stored.startswith(PLAIN_PREFIX):
                continue
            value = decode_secret(stored, None)
            if value is None:
                continue
            with contextlib.suppress(SecretEncryptionError):
                migrated[secret_id] = encode_secret(value, self._keystore)
        return migrated

    def get_secret(self, secret_id: str) -> str | None:
        return self._get_secret_from(self._data, secret_id)

    def _get_secret_from(self, data: dict[str, Any], secret_id: str) -> str | None:
        secrets = data["secrets"]
        stored = secrets.get(secret_id) if isinstance(secrets, dict) else None
        return decode_secret(stored, self._keystore)

    def answer_config(self) -> AnswerConfig:
        """Memory-only read — safe on the event loop. Reads the ACTIVE profile
        at answer time, so a profile switched mid-recording applies to that
        recording's answer (intended)."""
        data = self._data
        active = _active_profile(data)
        return AnswerConfig(
            resume=active["resume"],
            job_description=active["jobDescription"],
            style=data["answerStyle"],
            provider_id=data["llmProvider"],
            call_type=active["callType"],
            focus=active["focus"],
            notes=active["notes"],
        )

    def layout_mode(self) -> str:
        mode = self._data.get("layoutMode")
        return mode if isinstance(mode, str) and mode in LAYOUT_MODES else "full"

    def window_bounds(self, mode: str = "full") -> object:
        return self._data.get(_BOUNDS_KEYS.get(mode, "windowBounds"))

    def set_window_bounds(self, bounds: Mapping[str, int], mode: str = "full") -> None:
        """Geometry is cosmetic data: saving it must NEVER raise (some of
        these saves happen during shutdown). Each layout mode keeps its own
        geometry so the prompter strip and the full panel restore separately."""
        try:
            with self._lock:
                data = dict(self._data)
                data[_BOUNDS_KEYS.get(mode, "windowBounds")] = dict(bounds)
                self._save(data)
        except Exception:
            pass


# ------------------------------------------------------------- profiles


def _active_profile(data: dict[str, Any]) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = data["profiles"]
    wanted = data.get("activeProfileId")
    for profile in profiles:
        if profile["id"] == wanted:
            return profile
    return profiles[0]  # never raise on the loop: fall back to the first


def _sync_mirrors(data: dict[str, Any]) -> None:
    """Top-level resume/jobDescription mirror the active profile (write-only:
    an older build reads them, this build ignores them when profiles exist)."""
    active = _active_profile(data)
    data["resume"] = active["resume"]
    data["jobDescription"] = active["jobDescription"]


def _load_profile_text(value: object) -> str:
    # Stored VERBATIM — profile formatting is the user's.
    if isinstance(value, str) and len(value) <= MAX_PROFILE_CHARS:
        return value
    return ""


def _load_profiles(raw: object) -> list[dict[str, Any]]:
    """Per-field fallback for each entry; corrupt entries are skipped, corrupt
    ids get a fresh unique id, duplicate ids keep the first."""
    if not isinstance(raw, list):
        return []
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        profile_id = raw_id if isinstance(raw_id, str) and PROFILE_ID_RE.match(raw_id) else ""
        if not profile_id or profile_id in seen:
            if profile_id in seen:
                continue  # duplicate: first wins
            profile_id = _new_profile_id()
        raw_name = item.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name or len(name) > MAX_PROFILE_NAME_CHARS:
            name = f"Profile {index + 1}"
        call_type = item.get("callType")
        raw_focus = item.get("focus")
        profile = {
            "id": profile_id,
            "name": name,
            "callType": (
                call_type
                if isinstance(call_type, str) and call_type in CALL_TYPES
                else DEFAULT_CALL_TYPE
            ),
            "focus": (
                raw_focus
                if isinstance(raw_focus, str) and len(raw_focus) <= MAX_FOCUS_CHARS
                else ""
            ),
            "resume": _load_profile_text(item.get("resume")),
            "jobDescription": _load_profile_text(item.get("jobDescription")),
            "notes": _load_profile_text(item.get("notes")),
        }
        seen.add(profile_id)
        profiles.append(profile)
        if len(profiles) >= MAX_PROFILES:
            break
    return profiles


def _validate_profiles(raw: object) -> list[dict[str, Any]]:
    """Full-list replacement from the UI: all-or-nothing validation."""
    if not isinstance(raw, list) or not raw:
        raise AppError("internal", "Keep at least one profile.")
    if len(raw) > MAX_PROFILES:
        raise AppError("internal", f"Too many profiles (max {MAX_PROFILES}).")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AppError("internal", "Each profile must be an object.")
        raw_id = item.get("id")
        if raw_id is None or raw_id == "":
            profile_id = _new_profile_id()
        elif isinstance(raw_id, str) and PROFILE_ID_RE.match(raw_id):
            profile_id = raw_id
        else:
            raise AppError("internal", "Bad profile id.")
        if profile_id in seen:
            raise AppError("internal", "Duplicate profile id.")
        seen.add(profile_id)
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise AppError("internal", "Profile name is required.")
        name = raw_name.strip()
        if len(name) > MAX_PROFILE_NAME_CHARS:
            raise AppError(
                "internal", f"Profile name is too long (max {MAX_PROFILE_NAME_CHARS} characters)."
            )
        profiles.append(
            {
                "id": profile_id,
                "name": name,
                "callType": _require_call_type(item.get("callType", DEFAULT_CALL_TYPE)),
                "focus": _require_focus(item.get("focus", "")),
                "resume": _require_profile_text(item.get("resume", ""), "Resume"),
                "jobDescription": _require_profile_text(
                    item.get("jobDescription", ""), "Job description"
                ),
                "notes": _require_profile_text(item.get("notes", ""), "Notes"),
            }
        )
    return profiles


def _require_call_type(value: object) -> str:
    if not isinstance(value, str) or value not in CALL_TYPES:
        raise AppError("internal", "Unknown call type.")
    return value


def _require_focus(value: object) -> str:
    if not isinstance(value, str):
        raise AppError("internal", "Focus must be text.")
    if len(value) > MAX_FOCUS_CHARS:
        raise AppError("internal", f"Focus is too long (max {MAX_FOCUS_CHARS:,} characters).")
    return value


def _is_font_px(value: object, allowed: tuple[int, int]) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return allowed[0] <= value <= allowed[1]


REPLACE_ATTEMPTS = 5
REPLACE_RETRY_S = 0.02


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace with a short retry on Windows sharing violations.

    Antivirus real-time scanning and the search indexer open freshly written
    files for a few milliseconds; a rename that lands in that window fails
    with PermissionError even though nothing is wrong. Without the retry
    that surfaces as "Something went wrong inside the app core" on Save.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_S * (attempt + 1))


def _has_key_field(secret_id: str) -> str:
    return "has" + secret_id[:1].upper() + secret_id[1:] + "Key"


def _preservation_error() -> AppError:
    return AppError(
        "internal",
        "The settings file could not be read at startup, and a backup copy of "
        "it could not be made, so it was NOT overwritten. Check that the "
        "AICallAssistant folder in %APPDATA% is writable, then restart the app.",
    )


def _require_profile_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AppError("internal", f"{label} must be text.")
    if len(value) > MAX_PROFILE_CHARS:
        raise AppError("internal", f"{label} is too long (max 200,000 characters).")
    return value
