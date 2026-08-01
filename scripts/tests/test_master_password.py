#!/usr/bin/env python3
"""Tests for the master-password change route and the rotation launcher.

Covers /api/system/master-password (authenticated, deliberate change) and the
_launch_master_rotation helper it shares with /api/recovery/reset. The
behavioural difference between the two entry points is the point of most of
these: the change route must NOT pre-write MANAGER_PASSWORD and must NOT clear
the session, while the recovery route must do both.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_master_password.py
    pytest scripts/tests/test_master_password.py
"""
import os
import sys
import time
import stat
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import recovery             # noqa: E402

CURRENT_PW = "napped-plausible-sizzling-breeching-onyx"
NEW_PW = "quartz-lantern-mellow-drifting-cobalt-tundra"
PHRASE = "flatbed juggling payee equal tinsel humid"
FACTORY_PW = "device-label-42"


class Harness:
    """A test client over a throwaway .env, with the side-effecting calls spied.

    update_env_var and _launch_master_rotation are replaced by recorders: the
    assertions here are about *which* writes a route performs, and spying is
    both portable (real update_env_var shells out to GNU `sed -i`, which BSD
    sed rejects) and faster than letting a rotation thread start.
    """

    def __init__(self, with_recovery=True):
        fd, self.env_path = tempfile.mkstemp(prefix="hb_test_env_")
        os.close(fd)
        lines = [f"MANAGER_PASSWORD='{CURRENT_PW}'", f"MASTER_PASSWORD='{CURRENT_PW}'"]
        if with_recovery:
            for k, v in recovery.build_recovery_record(
                    PHRASE, recovery.DEFAULT_PHRASE_WORDS, time.time()).items():
                lines.append(f"{k}='{v}'")
        with open(self.env_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        self.env_writes = []
        self.rotations = []
        self._saved = {
            "ENV_FILE": hb.ENV_FILE,
            "update_env_var": hb.update_env_var,
            "_launch_master_rotation": hb._launch_master_rotation,
            "task": dict(hb.current_task_status),
            "limiter_enabled": hb.limiter.enabled,
        }
        hb.ENV_FILE = self.env_path
        hb.update_env_var = lambda k, v: self.env_writes.append((k, v))
        hb._launch_master_rotation = lambda pw: self.rotations.append(pw)
        hb.current_task_status.update({"status": "idle", "message": "", "log_type": "setup"})
        hb.limiter.enabled = False  # these tests exercise logic, not the buckets
        hb.app.config["TESTING"] = True
        self.client = hb.app.test_client()

    def login(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def authenticated(self):
        with self.client.session_transaction() as sess:
            return bool(sess.get("authenticated"))

    def change(self, current=CURRENT_PW, new=NEW_PW):
        return self.client.post("/api/system/master-password",
                                json={"current_password": current, "new_password": new})

    def close(self):
        hb.ENV_FILE = self._saved["ENV_FILE"]
        hb.update_env_var = self._saved["update_env_var"]
        hb._launch_master_rotation = self._saved["_launch_master_rotation"]
        hb.current_task_status.update(self._saved["task"])
        hb.limiter.enabled = self._saved["limiter_enabled"]
        os.unlink(self.env_path)


def test_change_requires_a_session():
    h = Harness()
    try:
        r = h.change()
        assert r.status_code == 401, r.status_code
        assert h.rotations == [], "rotation launched without a session"
    finally:
        h.close()


def test_change_rejects_wrong_current_password():
    h = Harness()
    h.login()
    try:
        r = h.change(current="not-the-password")
        assert r.status_code == 401, r.status_code
        assert h.rotations == [] and h.env_writes == []
    finally:
        h.close()


def test_change_rejects_unsafe_new_password():
    h = Harness()
    h.login()
    try:
        for bad in ["has spaces", "has'quote", "back\\slash", "short", ""]:
            r = h.change(new=bad)
            assert r.status_code == 400, f"{bad!r} -> {r.status_code}"
            assert recovery.NEW_PASSWORD_RULE in r.get_json()["error"]
        assert h.rotations == [] and h.env_writes == []
    finally:
        h.close()


def test_change_rejects_the_same_password():
    h = Harness()
    h.login()
    try:
        r = h.change(new=CURRENT_PW)
        assert r.status_code == 400, r.status_code
        assert h.rotations == [], "a no-op rotation was launched"
    finally:
        h.close()


def test_change_refuses_while_a_task_runs():
    h = Harness()
    h.login()
    try:
        hb.current_task_status["status"] = "running"
        r = h.change()
        assert r.status_code == 409, r.status_code
        assert h.rotations == []
    finally:
        h.close()


def test_change_launches_rotation_without_touching_env_or_session():
    """The two documented differences from the recovery path."""
    h = Harness()
    h.login()
    try:
        r = h.change()
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["status"] == "started"
        assert h.rotations == [NEW_PW], h.rotations
        # No pre-write: rotate_master_password.sh aborts before any .env change
        # if MariaDB fails, and that guarantee only holds if we stay out of it.
        assert h.env_writes == [], h.env_writes
        # No logout: the script rewrites MANAGER_PASSWORD partway through, so a
        # cleared session would strand the user in a window where neither
        # password works.
        assert h.authenticated(), "session was cleared"
    finally:
        h.close()


def test_recovery_reset_still_pre_writes_and_clears_session():
    """Regression guard on the shared-launcher refactor: the break-glass path
    keeps its opposite behaviour."""
    h = Harness()
    try:
        r = h.client.post("/api/recovery/reset",
                          json={"phrase": PHRASE, "new_password": NEW_PW},
                          headers={"Host": "192.168.178.58"})
        assert r.status_code == 200, r.get_json()
        assert h.rotations == [NEW_PW], h.rotations
        assert ("MANAGER_PASSWORD", NEW_PW) in h.env_writes, h.env_writes
        assert not h.authenticated(), "recovery must drop the stale session"
    finally:
        h.close()


def test_suggest_password_is_policy_valid_and_gated():
    h = Harness()
    try:
        assert h.client.get("/api/system/suggest-password").status_code == 401
        h.login()
        r = h.client.get("/api/system/suggest-password")
        assert r.status_code == 200, r.get_json()
        pw = r.get_json()["password"]
        assert recovery.is_valid_new_password(pw), pw
    finally:
        h.close()


class _StateOverride:
    """Pin is_setup_complete / is_handover_pending / get_factory_password.

    login() resolves these as module globals at call time, so swapping them is
    enough to place the box in any point of its lifecycle without touching the
    filesystem markers they normally read.
    """

    def __init__(self, setup_complete, handover_pending):
        self._saved = (hb.is_setup_complete, hb.is_handover_pending,
                       hb.get_factory_password)
        hb.is_setup_complete = lambda: setup_complete
        hb.is_handover_pending = lambda: handover_pending
        hb.get_factory_password = lambda: FACTORY_PW

    def restore(self):
        (hb.is_setup_complete, hb.is_handover_pending,
         hb.get_factory_password) = self._saved


def _login(harness, password):
    with harness.client.session_transaction() as sess:
        sess.clear()
    return harness.client.post("/login", data={"password": password})


def test_login_accepts_either_password_while_handover_is_pending():
    """Setup marks itself complete before the owner has read anything. Until
    they claim the credentials, the device-label password must still work — a
    session lost mid-install would otherwise lock them out of the only page
    that shows the master password and the recovery phrase."""
    h = Harness()
    state = _StateOverride(setup_complete=True, handover_pending=True)
    try:
        assert _login(h, FACTORY_PW).status_code == 200, "factory password refused"
        assert h.authenticated()
        assert _login(h, CURRENT_PW).status_code == 200, "master password refused"
        assert h.authenticated()
    finally:
        state.restore()
        h.close()


def test_login_rejects_the_factory_password_once_credentials_are_claimed():
    """The window closes on claim: from then on only the master password."""
    h = Harness()
    state = _StateOverride(setup_complete=True, handover_pending=False)
    try:
        assert _login(h, FACTORY_PW).status_code == 401, "factory password still worked"
        assert not h.authenticated()
        assert _login(h, CURRENT_PW).status_code == 200
        assert h.authenticated()
    finally:
        state.restore()
        h.close()


def test_launcher_hands_password_over_in_a_0600_file():
    """The real _launch_master_rotation: the password must reach the script in a
    private file, never on the command line."""
    captured = {}

    class FakeThread:
        def __init__(self, target=None, args=()):
            captured["args"] = args

        def start(self):
            pass

    saved_thread, saved_run = hb.threading.Thread, hb.subprocess.run
    hb.threading.Thread = FakeThread
    hb.subprocess.run = lambda *a, **k: None
    try:
        hb._launch_master_rotation(NEW_PW)
        _label, cmd, _log = captured["args"]
        path = [tok for tok in cmd.split() if "hb_rotate_" in tok][0].strip("'")
        assert NEW_PW not in cmd, "password leaked onto the command line"
        with open(path) as f:
            assert f.read() == NEW_PW
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        os.unlink(path)
    finally:
        hb.threading.Thread, hb.subprocess.run = saved_thread, saved_run


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
