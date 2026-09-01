"""Sealed household-member passwords.

Flask-free. The roster stays derived; this file is the one secret the
services cannot recompute. See docs/plans/HOUSEHOLD_ACCOUNTS.md §5.
"""
from __future__ import annotations

import os
import json
import base64
import secrets
import tempfile
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

ESCROW_PATH = "/var/lib/homebrain/member_escrow.json"
INFO = b"homebrain-member-escrow-v1"
NONCE_LEN = 12

_lock = threading.Lock()


class EscrowError(Exception):
    """Missing key, corrupt file, or AAD mismatch."""


def escrow_key(recovery_backup_key_b64):
    raw = base64.b64decode(recovery_backup_key_b64)
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=INFO).derive(raw)


def sealed_uids(path=ESCROW_PATH):
    return set((_load(path).get("wraps") or {}).keys())


def has_blob(uid, path=ESCROW_PATH):
    return uid in (_load(path).get("wraps") or {})


def open_password(uid, recovery_backup_key_b64, path=ESCROW_PATH):
    wraps = _load(path).get("wraps") or {}
    entry = wraps.get(uid)
    if not entry:
        raise EscrowError("no escrow for this person")
    try:
        nonce = base64.b64decode(entry["nonce"])
        ct = base64.b64decode(entry["ct"])
        plain = AESGCM(escrow_key(recovery_backup_key_b64)).decrypt(
            nonce, ct, uid.encode("utf-8"))
    except Exception as e:
        raise EscrowError("could not open escrow") from e
    return plain.decode("utf-8")


def seal(uid, password, recovery_backup_key_b64, path=ESCROW_PATH):
    """Lock covers the read as well as the write (invariant 14)."""
    with _lock:
        data = _load(path)
        wraps = data.setdefault("wraps", {})
        nonce = secrets.token_bytes(NONCE_LEN)
        ct = AESGCM(escrow_key(recovery_backup_key_b64)).encrypt(
            nonce, password.encode("utf-8"), uid.encode("utf-8"))
        wraps[uid] = {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
        }
        data["v"] = 1
        _atomic_write(path, data)


def drop(uid, path=ESCROW_PATH):
    with _lock:
        data = _load(path)
        wraps = data.get("wraps") or {}
        if uid not in wraps:
            return False
        del wraps[uid]
        data["wraps"] = wraps
        _atomic_write(path, data)
        return True


def rewrap(old_key_b64, new_key_b64, path=ESCROW_PATH):
    """Re-seal every entry under a new RECOVERY_BACKUP_KEY. All or nothing."""
    with _lock:
        data = _load(path)
        wraps = data.get("wraps") or {}
        if not wraps:
            return 0
        old_k = escrow_key(old_key_b64)
        new_k = escrow_key(new_key_b64)
        fresh = {}
        for uid, entry in wraps.items():
            try:
                nonce = base64.b64decode(entry["nonce"])
                ct = base64.b64decode(entry["ct"])
                password = AESGCM(old_k).decrypt(nonce, ct, uid.encode("utf-8"))
            except Exception as e:
                raise EscrowError(f"could not re-wrap {uid}") from e
            new_nonce = secrets.token_bytes(NONCE_LEN)
            new_ct = AESGCM(new_k).encrypt(new_nonce, password, uid.encode("utf-8"))
            fresh[uid] = {
                "nonce": base64.b64encode(new_nonce).decode("ascii"),
                "ct": base64.b64encode(new_ct).decode("ascii"),
            }
        data["wraps"] = fresh
        data["v"] = 1
        _atomic_write(path, data)
        return len(fresh)


def prune(live_uids, path=ESCROW_PATH):
    """Drop entries whose uid is on no service. Returns how many were removed."""
    live = set(live_uids)
    with _lock:
        data = _load(path)
        wraps = data.get("wraps") or {}
        stale = [uid for uid in wraps if uid not in live]
        if not stale:
            return 0
        for uid in stale:
            del wraps[uid]
        data["wraps"] = wraps
        _atomic_write(path, data)
        return len(stale)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"v": 1, "wraps": {}}
        data.setdefault("v", 1)
        data.setdefault("wraps", {})
        if not isinstance(data["wraps"], dict):
            data["wraps"] = {}
        return data
    except FileNotFoundError:
        return {"v": 1, "wraps": {}}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"v": 1, "wraps": {}}


def _atomic_write(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".member_escrow.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    import argparse
    import shutil
    parser = argparse.ArgumentParser(description="Member escrow restore re-wrap")
    parser.add_argument("command", choices=["restore-rewrap"])
    parser.add_argument("--json", required=True)
    parser.add_argument("--wrap-file", required=True)
    parser.add_argument("--dest-key-file", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.wrap_file, "r", encoding="utf-8") as f:
        old_key = f.read().strip()
    with open(args.dest_key_file, "r", encoding="utf-8") as f:
        dest_key = f.read().strip()
    if not old_key or not dest_key:
        raise SystemExit("wrap key or dest key missing")
    if os.path.abspath(args.json) != os.path.abspath(args.out):
        shutil.copy2(args.json, args.out)
        os.chmod(args.out, 0o600)
    n = rewrap(old_key, dest_key, path=args.out)
    print(n)

