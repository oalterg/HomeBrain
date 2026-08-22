#!/usr/bin/env python3
"""Unit tests for src/backup_crypto.py — HBK1 wrap/unwrap.

Runnable two ways (needs cryptography):
    python3 scripts/tests/test_backup_crypto.py
    pytest scripts/tests/test_backup_crypto.py
"""
import os
import sys
import json
import atexit
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import recovery           # noqa: E402
import backup_crypto as bc  # noqa: E402

MASTER = "correct-horse-battery-staple-quux"
PHRASE = "wobble tundra deputy chrome amulet salsa"
WRONG = "not-the-secret-at-all-nope"


_SCRATCH = []


def _tmp():
    """Scratch dir, swept at exit — these hold fixture keys and full archives,
    and leaving them behind makes a real 'no leftover secrets' check on a box
    impossible to read."""
    d = tempfile.mkdtemp(prefix="hbk1_")
    _SCRATCH.append(d)
    return d


@atexit.register
def _sweep_scratch():
    for d in _SCRATCH:
        shutil.rmtree(d, ignore_errors=True)


def _write(path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _secret_file(directory, name, text):
    path = os.path.join(directory, name)
    _write(path, (text + "\n").encode())
    return path


def _fake_gpg_body():
    # OpenPGP packets set the high bit; anything not starting HBK1 is fine.
    return b"\x8c" + os.urandom(256)


def _seal_archive(directory, body=None, with_recovery=True):
    rec = recovery.build_recovery_record(PHRASE, 6, 1_700_000_000)
    master_f = _secret_file(directory, "master", MASTER)
    dek_f = os.path.join(directory, "dek")
    hdr_f = os.path.join(directory, "hdr")
    extra = {}
    if with_recovery:
        extra["recovery_key_file"] = _secret_file(
            directory, "rk", rec["RECOVERY_BACKUP_KEY"])
        extra["recovery_salt_file"] = _secret_file(
            directory, "rs", rec["RECOVERY_BACKUP_SALT"])
    bc.seal_files(master_f, dek_f, hdr_f, **extra)
    with open(hdr_f, "rb") as f:
        header = f.read()
    body = body if body is not None else _fake_gpg_body()
    archive = os.path.join(directory, "homebrain_backup_test.tar.gz.gpg")
    _write(archive, header + body)
    return archive, rec, dek_f, body


def test_roundtrip_master_and_phrase():
    d = _tmp()
    archive, rec, dek_f, body = _seal_archive(d)
    with open(dek_f) as f:
        original_pass = f.read().strip()
    dek_m = bc.open_archive(archive, MASTER)
    dek_p = bc.open_archive(archive, PHRASE)
    assert dek_m == dek_p
    assert bc.dek_to_passphrase(dek_m) == original_pass
    try:
        bc.open_archive(archive, WRONG)
        assert False, "wrong secret opened the archive"
    except bc.BackupCryptoError:
        pass
    info = bc.inspect_archive(archive)
    assert info["format"] == "hbk1"
    assert info["unlock"] == "master_or_phrase"
    assert info["has_recovery"] and info["has_master"]


def test_phrase_normalize_opens():
    d = _tmp()
    archive, rec, _, _ = _seal_archive(d)
    dek = bc.open_archive(archive, "  " + PHRASE.upper() + "  ")
    assert len(dek) == bc.DEK_LEN


def test_master_only_has_no_recovery_wrap():
    d = _tmp()
    archive, rec, _, _ = _seal_archive(d, with_recovery=False)
    info = bc.inspect_archive(archive)
    assert info["unlock"] == "master"
    assert not info["has_recovery"]
    bc.open_archive(archive, MASTER)
    try:
        bc.open_archive(archive, PHRASE)
        assert False, "phrase opened a master-only wrap"
    except bc.BackupCryptoError:
        pass


def test_legacy_detect():
    d = _tmp()
    path = os.path.join(d, "legacy.tar.gz.gpg")
    body = _fake_gpg_body()
    _write(path, body)
    info = bc.inspect_archive(path)
    assert info["format"] == "legacy"
    assert info["unlock"] == "legacy"
    assert info["offset"] == 0


def test_rewrap_keeps_body_and_rotates_phrase():
    d = _tmp()
    archive, rec, _, body = _seal_archive(d)
    new_phrase = "alpha bravo charlie delta echo foxtrot"
    new_rec = recovery.backup_unlock_record(new_phrase)
    bc.rewrap_file(archive, MASTER, new_rec["RECOVERY_BACKUP_KEY"],
                   new_rec["RECOVERY_BACKUP_SALT"])
    info = bc.inspect_archive(archive)
    # Body after the new header must be identical.
    with open(archive, "rb") as f:
        f.seek(info["offset"])
        assert f.read() == body
    bc.open_archive(archive, MASTER)
    bc.open_archive(archive, new_phrase)
    try:
        bc.open_archive(archive, PHRASE)
        assert False, "old phrase still opened after rewrap"
    except bc.BackupCryptoError:
        pass


def test_truncated_header_fails_closed():
    d = _tmp()
    path = os.path.join(d, "trunc.tar.gz.gpg")
    _write(path, b"HBK1\n{\"v\":1")
    try:
        bc.read_header(path)
        assert False, "truncated header parsed"
    except bc.BackupCryptoError:
        pass
    info = bc.inspect_archive(path)
    assert info["format"] == "error"


def test_header_has_no_secrets():
    d = _tmp()
    archive, rec, _, _ = _seal_archive(d)
    with open(archive, "rb") as f:
        raw = f.read(4096)
    assert MASTER.encode() not in raw
    assert PHRASE.encode() not in raw
    assert rec["RECOVERY_BACKUP_KEY"].encode() not in raw
    header, _ = bc.read_header(archive)
    dumped = json.dumps(header)
    assert MASTER not in dumped
    assert rec["RECOVERY_BACKUP_KEY"] not in dumped


def test_cli_open_writes_matching_dek(tmp_path=None):
    d = _tmp()
    archive, rec, dek_f, _ = _seal_archive(d)
    with open(dek_f) as f:
        expected = f.read().strip()
    secret_f = _secret_file(d, "phrase", PHRASE)
    out_f = os.path.join(d, "opened")
    assert bc.main(["open", "--archive", archive, "--secret-file", secret_f,
                    "--dek-file", out_f]) == 0
    with open(out_f) as f:
        assert f.read().strip() == expected


def test_open_survives_a_scrypt_cost_bump():
    """An archive stays openable after the module defaults move.

    The header records the params it was sealed with; deriving under whatever
    the constants happen to be today would orphan every existing archive under
    BOTH secrets. Mirrors test_recovery's bump test for the verifier.
    """
    d = _tmp()
    archive, rec, _, _ = _seal_archive(d)
    saved = recovery.SCRYPT_N
    recovery.SCRYPT_N = saved * 2
    try:
        assert bc.open_archive(archive, MASTER)
        assert bc.open_archive(archive, PHRASE)
    finally:
        recovery.SCRYPT_N = saved


def test_rewrap_preserves_mtime_and_mode():
    """Retention sorts by mtime and the off-site mirror compares size+mtime, so
    a rewrap that restamps the file scrambles both."""
    d = _tmp()
    archive, rec, _, _ = _seal_archive(d)
    os.chmod(archive, 0o640)
    old = os.stat(archive)
    os.utime(archive, (old.st_atime - 90000, old.st_mtime - 90000))
    before = os.stat(archive)
    new_rec = recovery.backup_unlock_record("alpha bravo charlie delta echo foxtrot")
    bc.rewrap_file(archive, MASTER, new_rec["RECOVERY_BACKUP_KEY"],
                   new_rec["RECOVERY_BACKUP_SALT"])
    after = os.stat(archive)
    assert int(after.st_mtime) == int(before.st_mtime)
    assert after.st_mode & 0o777 == 0o640


def test_unknown_header_version_is_refused():
    d = _tmp()
    path = os.path.join(d, "future.tar.gz.gpg")
    _write(path, b'HBK1\n{"v":2,"alg":"gpg-aes256","wraps":{}}\n\n' + _fake_gpg_body())
    assert bc.inspect_archive(path)["format"] == "error"
    try:
        bc.open_archive(path, MASTER)
        assert False, "a v2 header was parsed as v1"
    except bc.BackupCryptoError:
        pass


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
