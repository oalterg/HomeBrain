#!/usr/bin/env python3
"""Tests for which credentials /start_setup seeds, and from what.

The bug these pin: MYSQL_PASSWORD used to be assigned the master password
alongside the five that genuinely derive from it. But rotate_master_password.sh
deliberately never rotates MYSQL_PASSWORD — occ needs DB access to rewrite its
own stored copy, so changing it there deadlocks — which meant the box's FIRST
master password stayed in .env in plaintext for the life of the machine. No
rotation cleared it, not even the recovery-phrase reset whose entire premise is
that the old password is compromised.

Found on a production box whose MYSQL_PASSWORD was still `admin` after six
rotations, because that was the master password the day it was set up.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_setup_credentials.py
    pytest scripts/tests/test_setup_credentials.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

MASTER_PW = "napped-plausible-sizzling-breeching-onyx"

# Every key that legitimately IS the master password. MYSQL_PASSWORD is
# deliberately absent — that is the whole point of these tests.
DERIVED_FROM_MASTER = {
    "MASTER_PASSWORD", "MANAGER_PASSWORD", "NEXTCLOUD_ADMIN_PASSWORD",
    "MYSQL_ROOT_PASSWORD", "HA_ADMIN_PASSWORD",
}


class Harness:
    """A test client over a throwaway .env, with side-effecting calls spied.

    update_env_var is a recorder rather than the real thing: the assertions are
    about *which* writes setup performs, and the real one shells out to GNU
    `sed -i`, which BSD sed rejects. The deploy thread is stubbed so the test
    never launches a real deployment.
    """

    def __init__(self, env_lines):
        fd, self.env_path = tempfile.mkstemp(prefix="hb_setup_env_")
        os.close(fd)
        with open(self.env_path, "w") as f:
            f.write("\n".join(env_lines) + "\n")
        fd, self.marker = tempfile.mkstemp(prefix="hb_setup_marker_")
        os.close(fd)
        fd, self.creds = tempfile.mkstemp(prefix="hb_setup_creds_")
        os.close(fd)

        self.writes = []
        self.threads = []
        self._saved = {k: getattr(hb, k) for k in (
            "ENV_FILE", "update_env_var", "is_setup_complete",
            "SETUP_STARTED_MARKER", "STAGING_CREDS_PATH", "get_factory_config")}
        self._saved_thread = hb.threading.Thread
        self._saved_limiter = hb.limiter.enabled

        hb.ENV_FILE = self.env_path
        hb.SETUP_STARTED_MARKER = self.marker
        hb.STAGING_CREDS_PATH = self.creds
        hb.update_env_var = lambda k, v: self.writes.append((k, v))
        hb.is_setup_complete = lambda: False
        hb.get_factory_config = lambda: {}
        hb.threading.Thread = lambda *a, **kw: self.threads.append((a, kw)) or _NoopThread()
        hb.limiter.enabled = False
        hb.app.config["TESTING"] = True
        self.client = hb.app.test_client()

    def start(self, **payload):
        # security_middleware gates ALL traffic on a session, including setup —
        # during setup that session comes from logging in with the factory
        # password. Without this the route returns the login gate, not a 200.
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        return self.client.post("/start_setup", json=payload or {"deployment_mode": "local"})

    def written(self, key):
        """Last value written for `key`, or None if it was never written."""
        vals = [v for k, v in self.writes if k == key]
        return vals[-1] if vals else None

    def close(self):
        for k, v in self._saved.items():
            setattr(hb, k, v)
        hb.threading.Thread = self._saved_thread
        hb.limiter.enabled = self._saved_limiter
        for p in (self.env_path, self.marker, self.creds):
            try:
                os.unlink(p)
            except OSError:
                pass


class _NoopThread:
    def start(self):
        pass


def _fresh():
    """A box mid-first-setup: master password chosen, DB password still empty
    exactly as config/.env.template ships it."""
    return Harness([f"MASTER_PASSWORD='{MASTER_PW}'", "MYSQL_PASSWORD="])


def test_mysql_password_is_not_the_master_password():
    h = _fresh()
    try:
        assert h.start().status_code == 200
        mysql = h.written("MYSQL_PASSWORD")
        assert mysql is not None, "MYSQL_PASSWORD was never seeded"
        assert mysql != MASTER_PW, (
            "MYSQL_PASSWORD is the master password — it can never be rotated, "
            "so this pins the first master password into .env forever")
    finally:
        h.close()


def test_mysql_password_is_a_strong_random_token():
    h = _fresh()
    try:
        h.start()
        mysql = h.written("MYSQL_PASSWORD")
        # Alphanumeric so it survives .env quoting, compose interpolation,
        # MariaDB and config.php without escaping.
        assert re.fullmatch(r"[0-9a-f]{32}", mysql), f"unexpected format: {mysql!r}"
    finally:
        h.close()


def test_two_setups_do_not_produce_the_same_db_password():
    # Strictly sequential: two live Harnesses would both patch
    # hb.update_env_var, so the second's recorder would swallow the first's
    # writes and the comparison would silently become `None != <value>` —
    # passing even on the unfixed code it exists to catch.
    seen = []
    for _ in range(2):
        h = _fresh()
        try:
            h.start()
            seen.append(h.written("MYSQL_PASSWORD"))
        finally:
            h.close()
    assert all(seen), f"a setup did not seed MYSQL_PASSWORD: {seen}"
    assert seen[0] != seen[1], "DB password is not random across boxes"


def test_the_other_five_credentials_still_are_the_master_password():
    """The decoupling must be surgical — these five are what the owner types
    or what rotation re-credentials, and they must keep tracking master."""
    h = _fresh()
    try:
        h.start()
        for key in sorted(DERIVED_FROM_MASTER):
            assert h.written(key) == MASTER_PW, f"{key} no longer tracks the master password"
    finally:
        h.close()


def test_an_existing_db_password_is_left_alone():
    """Re-running setup on a box whose DB volume already exists must not invent
    a password nextcloud_user does not have, or Nextcloud loses its database."""
    h = Harness([f"MASTER_PASSWORD='{MASTER_PW}'", "MYSQL_PASSWORD='already-provisioned'"])
    try:
        h.start()
        assert h.written("MYSQL_PASSWORD") is None, \
            "setup overwrote an existing DB password, orphaning the DB user"
    finally:
        h.close()


def test_a_legacy_box_whose_db_password_is_the_master_password_is_left_alone():
    """The .58 case. Repairing it needs a two-step through `occ`, so setup must
    not silently change it and strand Nextcloud — this fix stops the class for
    new boxes, it does not retrofit old ones."""
    h = Harness([f"MASTER_PASSWORD='{MASTER_PW}'", f"MYSQL_PASSWORD='{MASTER_PW}'"])
    try:
        h.start()
        assert h.written("MYSQL_PASSWORD") is None
    finally:
        h.close()


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
