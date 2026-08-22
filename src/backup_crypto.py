"""Envelope encryption for HomeBrain backup archives.

Each archive body is still GPG-symmetric (same flags as backup.sh). A short
HBK1 header holds two AES-GCM wraps of the GPG passphrase (the DEK): one
under the master password, one under the recovery-phrase wrap key. Either
secret opens the file. See docs/plans/BACKUP_UNLOCK.md.

Flask-free. cryptography is the only third-party import (already in
requirements.txt). Bash callers use this file as a CLI; the dashboard imports
the functions.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import recovery

MAGIC = b"HBK1\n"
ALG = "gpg-aes256"
NONCE_LEN = 12
DEK_LEN = 32
SALT_LEN = 16
HEADER_MAX = 65536


class BackupCryptoError(Exception):
    """Malformed header, missing wrap, or unreadable archive."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def _kdf_params() -> str:
    return (
        f"scrypt$n={recovery.SCRYPT_N}$r={recovery.SCRYPT_R}"
        f"$p={recovery.SCRYPT_P}$dklen={recovery.SCRYPT_DKLEN}"
    )


def _read_secret_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().rstrip("\r\n")


def _write_secret_file(path: str, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (text + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def dek_to_passphrase(dek: bytes) -> str:
    """ASCII GPG passphrase for a 32-byte DEK (hex, first-line-safe)."""
    return dek.hex()


def passphrase_to_dek(text: str) -> bytes:
    return bytes.fromhex(text.strip())


def _master_wrap_key(password: str, salt: bytes) -> bytes:
    # Do not normalize: master passwords are case-sensitive tokens.
    return hashlib_scrypt_raw(password, salt)


def hashlib_scrypt_raw(secret: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=recovery.SCRYPT_N,
        r=recovery.SCRYPT_R,
        p=recovery.SCRYPT_P,
        dklen=recovery.SCRYPT_DKLEN,
        maxmem=recovery.SCRYPT_MAXMEM,
    )


def _aes_wrap(key: bytes, plaintext: bytes) -> dict:
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return {"nonce": _b64(nonce), "ct": _b64(ct)}


def _aes_unwrap(key: bytes, wrap: dict) -> bytes | None:
    try:
        return AESGCM(key).decrypt(_unb64(wrap["nonce"]), _unb64(wrap["ct"]), None)
    except Exception:
        return None


def build_header(dek: bytes, master_password: str,
                 recovery_key_b64: str | None = None,
                 recovery_salt_b64: str | None = None) -> dict:
    master_salt = secrets.token_bytes(SALT_LEN)
    wraps = {
        "master": {
            "salt": _b64(master_salt),
            **_aes_wrap(_master_wrap_key(master_password, master_salt), dek),
        }
    }
    if recovery_key_b64 and recovery_salt_b64:
        wraps["recovery"] = {
            "salt": recovery_salt_b64,
            **_aes_wrap(_unb64(recovery_key_b64), dek),
        }
    return {
        "v": 1,
        "alg": ALG,
        "kdf": _kdf_params(),
        "wraps": wraps,
    }


def encode_header(header: dict) -> bytes:
    blob = json.dumps(header, separators=(",", ":"), ensure_ascii=True)
    return MAGIC + blob.encode("ascii") + b"\n\n"


def read_header(path: str) -> tuple[dict | None, int]:
    """Return (header, ciphertext_offset). header is None for a legacy GPG file."""
    with open(path, "rb") as f:
        magic = f.read(len(MAGIC))
        if magic != MAGIC:
            return None, 0
        buf = b""
        while True:
            chunk = f.read(1)
            if not chunk:
                raise BackupCryptoError("truncated HBK1 header")
            buf += chunk
            if buf.endswith(b"\n\n"):
                break
            if len(buf) > HEADER_MAX:
                raise BackupCryptoError("HBK1 header too large")
        try:
            header = json.loads(buf[:-2].decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupCryptoError(f"invalid HBK1 header: {exc}") from exc
        return header, f.tell()


def inspect_archive(path: str) -> dict:
    try:
        header, offset = read_header(path)
    except BackupCryptoError as exc:
        return {"format": "error", "error": str(exc), "unlock": "legacy"}
    if header is None:
        return {"format": "legacy", "offset": 0, "has_master": False,
                "has_recovery": False, "unlock": "legacy"}
    wraps = header.get("wraps") or {}
    has_master = "master" in wraps
    has_recovery = "recovery" in wraps
    if has_master and has_recovery:
        unlock = "master_or_phrase"
    elif has_master:
        unlock = "master"
    else:
        unlock = "legacy"
    return {
        "format": "hbk1",
        "offset": offset,
        "has_master": has_master,
        "has_recovery": has_recovery,
        "unlock": unlock,
        "v": header.get("v"),
    }


def unwrap_dek(header: dict, secret: str) -> bytes | None:
    wraps = header.get("wraps") or {}
    master = wraps.get("master")
    if master and master.get("salt"):
        key = _master_wrap_key(secret, _unb64(master["salt"]))
        dek = _aes_unwrap(key, master)
        if dek is not None:
            return dek
    rec = wraps.get("recovery")
    if rec and rec.get("salt"):
        derived = recovery.derive_backup_key(secret, rec["salt"])
        dek = _aes_unwrap(_unb64(derived["key"]), rec)
        if dek is not None:
            return dek
    return None


def open_archive(path: str, secret: str) -> bytes:
    """Return the DEK for an HBK1 archive, or raise BackupCryptoError."""
    header, _offset = read_header(path)
    if header is None:
        raise BackupCryptoError("not an HBK1 archive")
    dek = unwrap_dek(header, secret)
    if dek is None:
        raise BackupCryptoError("unwrap failed")
    return dek


def copy_body(path: str, dest) -> None:
    """Write the GPG ciphertext to dest (a file object). Legacy: the whole file."""
    header, offset = read_header(path)
    with open(path, "rb") as f:
        if header is not None:
            f.seek(offset)
        shutil.copyfileobj(f, dest, 1024 * 1024)


def seal_files(master_file: str, dek_file: str, header_file: str,
               recovery_key_file: str | None = None,
               recovery_salt_file: str | None = None) -> None:
    master = _read_secret_file(master_file)
    if not master:
        raise BackupCryptoError("master password file is empty")
    rec_key = rec_salt = None
    if recovery_key_file and recovery_salt_file:
        rec_key = _read_secret_file(recovery_key_file).strip()
        rec_salt = _read_secret_file(recovery_salt_file).strip()
        if not rec_key or not rec_salt:
            rec_key = rec_salt = None
    dek = secrets.token_bytes(DEK_LEN)
    header = build_header(dek, master, rec_key, rec_salt)
    _write_secret_file(dek_file, dek_to_passphrase(dek))
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(header_file, flags, 0o600)
    try:
        os.write(fd, encode_header(header))
    finally:
        os.close(fd)


def open_to_dek_file(archive: str, secret_file: str, dek_file: str) -> None:
    secret = _read_secret_file(secret_file)
    dek = open_archive(archive, secret)
    _write_secret_file(dek_file, dek_to_passphrase(dek))


def rewrap_file(path: str, master_password: str,
                recovery_key_b64: str, recovery_salt_b64: str) -> None:
    """Replace wraps.recovery; leave the GPG body untouched."""
    header, offset = read_header(path)
    if header is None:
        raise BackupCryptoError("not an HBK1 archive")
    dek = unwrap_dek(header, master_password)
    if dek is None:
        raise BackupCryptoError("master wrap did not open; cannot rewrap")
    wraps = dict(header.get("wraps") or {})
    wraps["recovery"] = {
        "salt": recovery_salt_b64,
        **_aes_wrap(_unb64(recovery_key_b64), dek),
    }
    header = dict(header)
    header["wraps"] = wraps
    new_header = encode_header(header)
    directory = os.path.dirname(path) or "."
    fd, tmp = _mkstemp_in(directory)
    try:
        os.write(fd, new_header)
        with open(path, "rb") as inf:
            inf.seek(offset)
            while True:
                chunk = inf.read(1024 * 1024)
                if not chunk:
                    break
                os.write(fd, chunk)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = ""
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _mkstemp_in(directory: str) -> tuple[int, str]:
    import tempfile
    return tempfile.mkstemp(prefix=".hbk1_", suffix=".tmp", dir=directory)


def _cmd_seal(args):
    seal_files(args.master_file, args.dek_file, args.header_file,
               args.recovery_key_file, args.recovery_salt_file)


def _cmd_open(args):
    open_to_dek_file(args.archive, args.secret_file, args.dek_file)


def _cmd_inspect(args):
    info = inspect_archive(args.archive)
    if args.field:
        val = info.get(args.field)
        if val is None:
            raise BackupCryptoError(f"no field {args.field!r}")
        sys.stdout.write("true" if val is True else "false" if val is False else str(val))
        sys.stdout.write("\n")
        return
    json.dump(info, sys.stdout)
    sys.stdout.write("\n")


def _cmd_copy_body(args):
    copy_body(args.archive, sys.stdout.buffer)


def _cmd_rewrap(args):
    master = _read_secret_file(args.master_file)
    rec_key = _read_secret_file(args.recovery_key_file).strip()
    rec_salt = _read_secret_file(args.recovery_salt_file).strip()
    rewrap_file(args.archive, master, rec_key, rec_salt)


def main(argv=None):
    p = argparse.ArgumentParser(prog="backup_crypto.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal")
    s.add_argument("--master-file", required=True)
    s.add_argument("--dek-file", required=True)
    s.add_argument("--header-file", required=True)
    s.add_argument("--recovery-key-file")
    s.add_argument("--recovery-salt-file")
    s.set_defaults(func=_cmd_seal)

    s = sub.add_parser("open")
    s.add_argument("--archive", required=True)
    s.add_argument("--secret-file", required=True)
    s.add_argument("--dek-file", required=True)
    s.set_defaults(func=_cmd_open)

    s = sub.add_parser("inspect")
    s.add_argument("--archive", required=True)
    s.add_argument("--field")
    s.set_defaults(func=_cmd_inspect)

    s = sub.add_parser("copy-body")
    s.add_argument("--archive", required=True)
    s.set_defaults(func=_cmd_copy_body)

    s = sub.add_parser("rewrap")
    s.add_argument("--archive", required=True)
    s.add_argument("--master-file", required=True)
    s.add_argument("--recovery-key-file", required=True)
    s.add_argument("--recovery-salt-file", required=True)
    s.set_defaults(func=_cmd_rewrap)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except BackupCryptoError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
