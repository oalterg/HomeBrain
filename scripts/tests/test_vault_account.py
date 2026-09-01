"""Tests for src/vault_account.py."""
import os
import sys
import base64

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import vault_account as va  # noqa: E402

EMAIL = "alex@homebrain.local"
PASSWORD = "correct-horse-battery-staple-copper-wren"
# Captured from hashlib PBKDF2-HMAC-SHA256 (600k then 1), email-as-salt.
# A Bitwarden client that disagrees has a different KDF, not a different hash.
GOLDEN_HASH = "7KN3ZHy27rYurdIXXScRfkqVfN/x3vuMAgGMmJuahiQ="


def test_email_is_lowercased():
    assert va.vault_email("Alex") == EMAIL
    assert va.vault_email("  ALEX  ") == EMAIL


def test_master_password_hash_is_stable():
    h, mk = va.master_password_hash(PASSWORD, EMAIL)
    assert h == GOLDEN_HASH
    assert len(mk) == 32
    again, _ = va.master_password_hash(PASSWORD, EMAIL)
    assert again == h


def test_email_case_does_not_change_the_hash():
    a, _ = va.master_password_hash(PASSWORD, "Alex@HomeBrain.Local")
    b, _ = va.master_password_hash(PASSWORD, EMAIL)
    assert a == b


def test_encstring_roundtrip():
    under = va.stretch(os.urandom(32))
    plain = os.urandom(64)
    blob = va.encstring2(plain, under)
    parts = blob[2:].split("|")
    assert blob.startswith("2.")
    assert len(parts) == 3
    assert len(base64.b64decode(parts[0])) == 16
    assert va.decstring2(blob, under) == plain


def test_register_payload_roundtrip():
    payload, user_key = va.register_payload("alex", "Alex", PASSWORD)
    assert payload["email"] == EMAIL
    assert payload["kdf"] == 0
    assert payload["kdfIterations"] == 600_000
    assert payload["masterPasswordHint"] is None
    _, mk = va.master_password_hash(PASSWORD, EMAIL)
    assert va.decstring2(payload["key"], va.stretch(mk)) == user_key
    priv = va.decstring2(payload["keys"]["encryptedPrivateKey"], user_key)
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    key = load_der_private_key(priv, password=None)
    assert key.key_size == 2048


def test_extract_expand_does_not_match_stretch():
    mk = va.master_key(PASSWORD, EMAIL)
    assert va.stretch(mk) != va.stretch_extract_expand(mk)
    user_key = os.urandom(64)
    blob = va.encstring2(user_key, va.stretch_extract_expand(mk))
    try:
        va.decstring2(blob, va.stretch(mk))
        assert False, "wrong stretch still opened the key"
    except va.VaultAccountError:
        pass


def test_password_change_keeps_user_key():
    payload, user_key = va.register_payload("alex", "Alex", PASSWORD)
    body = va.password_change_payload(
        PASSWORD, "new-correct-horse-battery-staple", EMAIL, payload["key"])
    _, new_mk = va.master_password_hash(
        "new-correct-horse-battery-staple", EMAIL)
    assert va.decstring2(body["key"], va.stretch(new_mk)) == user_key
    assert body["masterPasswordHash"] == payload["masterPasswordHash"]


def test_argon2_kdf_is_refused():
    payload, _ = va.register_payload("alex", "Alex", PASSWORD)
    try:
        va.password_change_payload(
            PASSWORD, "other-password-word-word-word-word", EMAIL,
            payload["key"], kdf=1)
        assert False, "argon2 should be refused"
    except va.VaultAccountError as e:
        assert "did not issue" in str(e)


def test_probe_verdict():
    assert va.probe_verdict(200) == "recoverable"
    assert va.probe_verdict(401) == "not_recoverable"
    assert va.probe_verdict(400) == "not_recoverable"
    assert va.probe_verdict(429) == "unknown"
    assert va.probe_verdict(0) == "unknown"
    assert va.probe_verdict(503) == "unknown"
