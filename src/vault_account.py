"""Bitwarden client-side crypto for household vault accounts.

Flask-free. stdlib + cryptography. See docs/plans/HOUSEHOLD_ACCOUNTS.md §3.4.

Stretched keys use RFC 5869 HKDF-Expand only (Bitwarden's hkdfExpand).
HKDF Extract-then-Expand produces a register that 200s and a vault nobody
can open.
"""
from __future__ import annotations

import os
import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import hashes, serialization, padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

KDF_PBKDF2 = 0
KDF_ITERATIONS = 600_000
DOMAIN = "homebrain.local"


class VaultAccountError(Exception):
    """Malformed EncString, MAC failure, or a KDF we did not issue."""


def vault_email(uid):
    return f"{uid.strip().lower()}@{DOMAIN}"


def master_key(password, email, iterations=KDF_ITERATIONS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        email.strip().lower().encode("utf-8"), iterations, dklen=32)


def master_password_hash(password, email, iterations=KDF_ITERATIONS):
    """Client hash sent as masterPasswordHash: b64(PBKDF2(master_key, password, 1))."""
    mk = master_key(password, email, iterations)
    digest = hashlib.pbkdf2_hmac("sha256", mk, password.encode("utf-8"), 1, dklen=32)
    return base64.b64encode(digest).decode("ascii"), mk


def stretch(mk):
    """32-byte enc key || 32-byte mac key. Expand only — not HKDF()."""
    enc = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"enc").derive(mk)
    mac = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"mac").derive(mk)
    return enc + mac


def stretch_extract_expand(mk):
    """Wrong stretch. Tests pin that this does not match Bitwarden."""
    enc = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=b"enc").derive(mk)
    mac = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=b"mac").derive(mk)
    return enc + mac


def encstring2(plain, under):
    enc_key, mac_key = under[:32], under[32:]
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    return "2." + _b64(iv) + "|" + _b64(ct) + "|" + _b64(tag)


def decstring2(blob, under):
    if not blob or not blob.startswith("2."):
        raise VaultAccountError("unsupported EncString")
    try:
        iv_b, ct_b, mac_b = blob[2:].split("|")
        iv, ct, mac = _unb64(iv_b), _unb64(ct_b), _unb64(mac_b)
    except (ValueError, Exception) as e:
        raise VaultAccountError("malformed EncString") from e
    if len(iv) != 16:
        raise VaultAccountError("EncString iv must be 16 bytes")
    enc_key, mac_key = under[:32], under[32:]
    expected = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise VaultAccountError("EncString MAC failed")
    decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def register_payload(uid, name, password, *, user_key=None):
    """Body for POST /identity/accounts/register. user_key is 64 random bytes."""
    email = vault_email(uid)
    mp_hash, mk = master_password_hash(password, email)
    if user_key is None:
        user_key = os.urandom(64)
    if len(user_key) != 64:
        raise VaultAccountError("user_key must be 64 bytes")
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = rsa_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    private_der = rsa_key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    return {
        "email": email,
        "name": name or uid,
        "masterPasswordHash": mp_hash,
        "masterPasswordHint": None,
        "key": encstring2(user_key, stretch(mk)),
        "keys": {
            "publicKey": _b64(public_der),
            "encryptedPrivateKey": encstring2(private_der, user_key),
        },
        "kdf": KDF_PBKDF2,
        "kdfIterations": KDF_ITERATIONS,
    }, user_key


def password_change_payload(old_password, new_password, email, encrypted_key,
                            *, kdf=KDF_PBKDF2, kdf_iterations=KDF_ITERATIONS):
    """Body for POST /accounts/password. encrypted_key is the login response Key."""
    if kdf != KDF_PBKDF2:
        raise VaultAccountError(
            "this vault uses key settings we did not issue")
    old_hash, old_mk = master_password_hash(old_password, email, kdf_iterations)
    user_key = decstring2(encrypted_key, stretch(old_mk))
    # The account's own iteration count, not ours. Vaultwarden's password
    # change rewrites the hash and the key but leaves client_kdf_iter alone,
    # so deriving the new pair at our default would hand back a hash the
    # client never computes and a key it cannot unwrap — a bricked vault on
    # any account that is not already at KDF_ITERATIONS.
    new_hash, new_mk = master_password_hash(new_password, email, kdf_iterations)
    return {
        "masterPasswordHash": old_hash,
        "newMasterPasswordHash": new_hash,
        "key": encstring2(user_key, stretch(new_mk)),
    }


def probe_verdict(status_code):
    """Map connect/token HTTP status to the three-way recoverable verdict."""
    if status_code == 200:
        return "recoverable"
    if status_code in (400, 401):
        return "not_recoverable"
    return "unknown"


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text)
