"""The settings file: validation, atomic writes, write-only secrets.

The file is user-writable and survives upgrades — untrusted input. Every
field falls back to its default INDIVIDUALLY: one corrupt value must never
cost the user their resume or keys. All I/O here is synchronous; async
callers wrap calls in `asyncio.to_thread` (file writes and DPAPI must never
run on the event loop).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app_core.errors import AppError
from app_core.llm.base import ProviderRegistry
from app_core.session.machine import DEEPGRAM_SECRET_ID, AnswerConfig
from app_core.store.secrets import Keystore, decode_secret, encode_secret

MAX_PROFILE_CHARS = 200_000
MAX_HOTKEY_CHARS = 100
DEFAULT_HOTKEY = "Ctrl+Shift+Space"
ANSWER_STYLES = ("brief", "balanced", "detailed")


class SettingsStore:
    def __init__(
        self, path: Path, keystore: Keystore | None, registry: ProviderRegistry
    ) -> None:
        self._path = path
        self._keystore = keystore
        self._registry = registry
        self._data = self._load()

    # -------------------------------------------------------------- loading

    def _defaults(self) -> dict[str, Any]:
        provider_ids = self._registry.ids()
        default_provider = "anthropic" if "anthropic" in provider_ids else (
            provider_ids[0] if provider_ids else "anthropic"
        )
        return {
            "resume": "",
            "jobDescription": "",
            "alwaysOnTop": True,
            "llmProvider": default_provider,
            "answerStyle": "balanced",
            "hotkey": DEFAULT_HOTKEY,
            "secrets": {},
            "windowBounds": None,
        }

    def _load(self) -> dict[str, Any]:
        defaults = self._defaults()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # Missing or unparseable file loads as first-run defaults, never
            # a crash.
            return defaults
        if not isinstance(raw, dict):
            return defaults
        data = dict(defaults)
        resume = raw.get("resume")
        if isinstance(resume, str) and len(resume) <= MAX_PROFILE_CHARS:
            data["resume"] = resume  # stored VERBATIM — profile formatting is the user's
        jd = raw.get("jobDescription")
        if isinstance(jd, str) and len(jd) <= MAX_PROFILE_CHARS:
            data["jobDescription"] = jd
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
        secrets = raw.get("secrets")
        if isinstance(secrets, dict):
            data["secrets"] = {
                k: v for k, v in secrets.items() if isinstance(k, str) and isinstance(v, str)
            }
        bounds = raw.get("windowBounds")
        if isinstance(bounds, dict):
            data["windowBounds"] = bounds  # sanitized at restore time (bounds.py)
        return data

    # -------------------------------------------------------------- writing

    def _save(self, data: dict[str, Any]) -> None:
        """Atomic write: tmp file then os.replace. A crash or full disk
        mid-write must not truncate the file into "defaults". The in-memory
        cache updates only AFTER the write lands, so a failed write leaves
        memory matching disk."""
        tmp = Path(str(self._path) + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        self._data = data

    # ------------------------------------------------------------------ API

    def view(self) -> dict[str, Any]:
        """What the frontend sees. NEVER includes key material — only
        per-provider booleans, generated from the registry so a new provider
        gets its key field and first-run nudge for free."""
        out: dict[str, Any] = {
            "resume": self._data["resume"],
            "jobDescription": self._data["jobDescription"],
            "alwaysOnTop": self._data["alwaysOnTop"],
            "llmProvider": self._data["llmProvider"],
            "answerStyle": self._data["answerStyle"],
            "hotkey": self._data["hotkey"],
            "providers": self._registry.choices(),
        }
        for secret_id in [DEEPGRAM_SECRET_ID, *self._registry.ids()]:
            out[_has_key_field(secret_id)] = self.get_secret(secret_id) is not None
        return out

    def patch(self, patch: Mapping[str, object]) -> dict[str, Any]:
        """Validated patch; returns the fresh view. Invalid values raise
        AppError (the bridge turns it into an error Result envelope)."""
        data = dict(self._data)
        if "resume" in patch:
            data["resume"] = _require_profile_text(patch["resume"], "Resume")
        if "jobDescription" in patch:
            data["jobDescription"] = _require_profile_text(
                patch["jobDescription"], "Job description"
            )
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
                if not trimmed_key:
                    # An empty (or whitespace-only) key value CLEARS the
                    # stored key; omitting the field leaves it untouched.
                    secrets.pop(secret_id, None)
                else:
                    secrets[secret_id] = encode_secret(trimmed_key, self._keystore)
            data["secrets"] = secrets
        self._save(data)
        return self.view()

    def get_secret(self, secret_id: str) -> str | None:
        secrets = self._data["secrets"]
        stored = secrets.get(secret_id) if isinstance(secrets, dict) else None
        return decode_secret(stored, self._keystore)

    def answer_config(self) -> AnswerConfig:
        """Memory-only read — safe on the event loop."""
        return AnswerConfig(
            resume=self._data["resume"],
            job_description=self._data["jobDescription"],
            style=self._data["answerStyle"],
            provider_id=self._data["llmProvider"],
        )

    def window_bounds(self) -> object:
        return self._data.get("windowBounds")

    def set_window_bounds(self, bounds: Mapping[str, int]) -> None:
        """Geometry is cosmetic data: saving it must NEVER raise (some of
        these saves happen during shutdown)."""
        try:
            data = dict(self._data)
            data["windowBounds"] = dict(bounds)
            self._save(data)
        except Exception:
            pass


def _has_key_field(secret_id: str) -> str:
    return "has" + secret_id[:1].upper() + secret_id[1:] + "Key"


def _require_profile_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AppError("internal", f"{label} must be text.")
    if len(value) > MAX_PROFILE_CHARS:
        raise AppError("internal", f"{label} is too long (max 200,000 characters).")
    return value
