"""Secret encode/decode semantics (with a fake keystore — no DPAPI in tests)."""

from __future__ import annotations

import base64

import pytest

from app_core.store.secrets import Keystore, decode_secret, encode_secret


class XorKeystore:
    """Deterministic stand-in for DPAPI. Like DPAPI, it refuses blobs it did
    not produce (the magic prefix stands in for the DPAPI envelope)."""

    MAGIC = b"XKS1:"

    def protect(self, data: bytes) -> bytes:
        return self.MAGIC + bytes(b ^ 0x5A for b in data)

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(self.MAGIC):
            raise ValueError("not a blob from this keystore")
        return bytes(b ^ 0x5A for b in data[len(self.MAGIC) :])


class BrokenKeystore:
    def protect(self, data: bytes) -> bytes:
        raise OSError("keystore unavailable")

    def unprotect(self, data: bytes) -> bytes:
        raise OSError("keystore unavailable")


@pytest.fixture
def ks() -> Keystore:
    return XorKeystore()


class TestEncode:
    def test_roundtrip_encrypted(self, ks: Keystore) -> None:
        stored = encode_secret("sk-abc123", ks)
        assert stored.startswith("enc:")
        assert decode_secret(stored, ks) == "sk-abc123"

    def test_keystore_unavailable_falls_back_to_marked_plain(self) -> None:
        stored = encode_secret("sk-abc123", None)
        assert stored.startswith("plain:")
        assert decode_secret(stored, None) == "sk-abc123"

    def test_failing_keystore_falls_back_to_marked_plain(self) -> None:
        stored = encode_secret("sk-abc123", BrokenKeystore())
        assert stored.startswith("plain:")

    def test_key_material_never_stored_raw(self, ks: Keystore) -> None:
        stored = encode_secret("sk-abc123", ks)
        assert "sk-abc123" not in stored

    def test_unicode_keys_survive(self, ks: Keystore) -> None:
        assert decode_secret(encode_secret("clé-秘密", ks), ks) == "clé-秘密"


class TestDecode:
    def test_decodes_by_stored_prefix_not_keystore_availability(self, ks: Keystore) -> None:
        # A plain: value decodes fine even though a keystore exists now.
        plain = "plain:" + base64.b64encode(b"legacy-key").decode()
        assert decode_secret(plain, ks) == "legacy-key"

    def test_enc_value_without_keystore_reads_as_unset(self, ks: Keystore) -> None:
        stored = encode_secret("k", ks)
        assert decode_secret(stored, None) is None

    def test_undecryptable_reads_as_unset(self) -> None:
        # e.g. settings file copied from another machine — fail closed.
        stored = "enc:" + base64.b64encode(b"garbage-from-elsewhere").decode()
        assert decode_secret(stored, BrokenKeystore()) is None

    def test_unknown_prefix_reads_as_unset(self, ks: Keystore) -> None:
        # The raw stored string must never reach a provider.
        assert decode_secret("v2:whatever", ks) is None
        assert decode_secret("sk-raw-key-no-prefix", ks) is None

    def test_invalid_base64_reads_as_unset(self, ks: Keystore) -> None:
        assert decode_secret("enc:!!!not-base64!!!", ks) is None
        assert decode_secret("plain:!!!", ks) is None

    def test_non_string_reads_as_unset(self, ks: Keystore) -> None:
        for bad in (None, 5, ["enc:x"], {"enc": "x"}):
            assert decode_secret(bad, ks) is None
