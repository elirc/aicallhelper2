"""API-key encryption: Windows DPAPI, failing CLOSED.

New values are always stored as `enc:<base64 of DPAPI blob>`. When the OS
keystore is unavailable or refuses, `encode_secret` raises
`SecretEncryptionError` — the caller must fail the save visibly; a key is
never silently written in a recoverable form while the UI says encrypted.

`plain:<base64>` values written by older builds (which fell back silently)
still DECODE, so an upgrade never loses a working key, and
`secret_storage` reports them as "plaintext" so the UI can say so; the
settings store re-encrypts them on the next successful save. Decoding goes
by the STORED prefix, not by current keystore availability. Undecryptable
values (e.g. a settings file copied from another machine) or unknown
prefixes read as unset — never hand the raw stored string to a provider.
"""

from __future__ import annotations

import base64
from typing import Literal, Protocol

ENC_PREFIX = "enc:"
PLAIN_PREFIX = "plain:"

SecretStorage = Literal["encrypted", "plaintext"]


class SecretEncryptionError(Exception):
    """The OS keystore could not encrypt a secret; nothing was encoded."""


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
    """`enc:` + DPAPI blob. Raises SecretEncryptionError rather than ever
    falling back to a recoverable encoding."""
    if keystore is None:
        raise SecretEncryptionError("no keystore available")
    try:
        protected = keystore.protect(value.encode("utf-8"))
    except Exception as exc:
        raise SecretEncryptionError(str(exc) or type(exc).__name__) from exc
    return ENC_PREFIX + base64.b64encode(protected).decode("ascii")


def secret_storage(stored: object) -> SecretStorage | None:
    """How a stored value is protected on disk, by its prefix alone."""
    if isinstance(stored, str):
        if stored.startswith(ENC_PREFIX):
            return "encrypted"
        if stored.startswith(PLAIN_PREFIX):
            return "plaintext"
    return None


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
