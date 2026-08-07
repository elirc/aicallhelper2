"""API-key encryption: Windows DPAPI with an honestly-labeled fallback.

Stored values are `enc:<base64 of DPAPI blob>` or, when the OS keystore is
unavailable, `plain:<base64>` — MARKED, still functional. Decoding goes by
the STORED prefix, not by current keystore availability. Undecryptable
values (e.g. a settings file copied from another machine) or unknown
prefixes read as unset — fail closed, never hand the raw stored string to a
provider.
"""

from __future__ import annotations

import base64
from typing import Protocol

ENC_PREFIX = "enc:"
PLAIN_PREFIX = "plain:"


class Keystore(Protocol):
    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


class DpapiKeystore:
    """The real Windows DPAPI keystore (current-user scope)."""

    def protect(self, data: bytes) -> bytes:
        import win32crypt

        blob = win32crypt.CryptProtectData(data, None, None, None, None, 0)
        return bytes(blob)

    def unprotect(self, data: bytes) -> bytes:
        import win32crypt

        _description, plaintext = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        return bytes(plaintext)


def encode_secret(value: str, keystore: Keystore | None) -> str:
    raw = value.encode("utf-8")
    if keystore is not None:
        try:
            protected = keystore.protect(raw)
            return ENC_PREFIX + base64.b64encode(protected).decode("ascii")
        except Exception:
            pass  # keystore unavailable -> marked plaintext fallback below
    return PLAIN_PREFIX + base64.b64encode(raw).decode("ascii")


def decode_secret(stored: object, keystore: Keystore | None) -> str | None:
    """Decode a stored secret; None means "unset" (missing, corrupt, foreign)."""
    if not isinstance(stored, str):
        return None
    if stored.startswith(ENC_PREFIX):
        if keystore is None:
            return None
        try:
            blob = base64.b64decode(stored[len(ENC_PREFIX) :], validate=True)
            return keystore.unprotect(blob).decode("utf-8")
        except Exception:
            return None
    if stored.startswith(PLAIN_PREFIX):
        try:
            raw = base64.b64decode(stored[len(PLAIN_PREFIX) :], validate=True)
            return raw.decode("utf-8")
        except Exception:
            return None
    return None  # unknown prefix reads as unset — fail closed
