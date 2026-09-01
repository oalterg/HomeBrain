"""Tests for src/member_escrow.py."""
import os
import sys
import json
import base64
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import member_escrow as me  # noqa: E402


def _key():
    return base64.b64encode(os.urandom(32)).decode("ascii")


def test_seal_open_roundtrip(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    key = _key()
    me.seal("alex", "correct-horse-battery-staple", key, path=path)
    assert me.has_blob("alex", path=path)
    assert me.open_password("alex", key, path=path) == "correct-horse-battery-staple"
    st = os.stat(path)
    assert st.st_mode & 0o777 == 0o600


def test_aad_binds_uid(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    key = _key()
    me.seal("alex", "secret-password-word-word", key, path=path)
    data = json.loads(open(path).read())
    data["wraps"]["sam"] = data["wraps"]["alex"]
    open(path, "w").write(json.dumps(data))
    try:
        me.open_password("sam", key, path=path)
        assert False, "swapped AAD opened"
    except me.EscrowError:
        pass


def test_rewrap_invalidates_old_key(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    old = _key()
    new = _key()
    me.seal("alex", "pw-one-two-three-four-five", old, path=path)
    me.seal("sam", "pw-six-seven-eight-nine-ten", old, path=path)
    n = me.rewrap(old, new, path=path)
    assert n == 2
    assert me.open_password("alex", new, path=path) == "pw-one-two-three-four-five"
    try:
        me.open_password("alex", old, path=path)
        assert False, "old key still opened"
    except me.EscrowError:
        pass


def test_absent_file_is_empty_not_a_crash(tmp_path):
    path = str(tmp_path / "missing.json")
    assert me.sealed_uids(path=path) == set()
    assert me.has_blob("alex", path=path) is False


def test_truncated_file_degrades(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    open(path, "w").write("{not json")
    assert me.sealed_uids(path=path) == set()


def test_concurrent_seals_both_survive(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    key = _key()
    errors = []

    def one(uid, pw):
        try:
            me.seal(uid, pw, key, path=path)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=one, args=("alex", "pw-alex-one-two-three-four"))
    t2 = threading.Thread(target=one, args=("sam", "pw-sam-five-six-seven-eight"))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []
    assert me.open_password("alex", key, path=path) == "pw-alex-one-two-three-four"
    assert me.open_password("sam", key, path=path) == "pw-sam-five-six-seven-eight"


def test_prune_drops_orphans(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    key = _key()
    me.seal("alex", "pw-alex-one-two-three-four", key, path=path)
    me.seal("ghost", "pw-ghost-one-two-three-four", key, path=path)
    n = me.prune({"alex"}, path=path)
    assert n == 1
    assert me.has_blob("alex", path=path)
    assert not me.has_blob("ghost", path=path)


def test_drop_uid(tmp_path):
    path = str(tmp_path / "member_escrow.json")
    key = _key()
    me.seal("alex", "pw-alex-one-two-three-four", key, path=path)
    assert me.drop("alex", path=path) is True
    assert not me.has_blob("alex", path=path)
    assert me.drop("alex", path=path) is False


def test_restore_rewrap_cli(tmp_path):
    src = str(tmp_path / "member_escrow.json")
    dest = str(tmp_path / "out.json")
    wrap = str(tmp_path / "member_escrow.wrap")
    dest_key_file = str(tmp_path / "dest.key")
    old = _key()
    new = _key()
    me.seal("alex", "pw-alex-restore-cli-roundtrip", old, path=src)
    open(wrap, "w").write(old)
    open(dest_key_file, "w").write(new)
    import subprocess
    r = subprocess.run(
        [sys.executable, me.__file__, "restore-rewrap",
         "--json", src, "--wrap-file", wrap,
         "--dest-key-file", dest_key_file, "--out", dest],
        capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    assert me.open_password("alex", new, path=dest) == "pw-alex-restore-cli-roundtrip"
    try:
        me.open_password("alex", old, path=dest)
        assert False, "old wrap key still opened dest"
    except me.EscrowError:
        pass
