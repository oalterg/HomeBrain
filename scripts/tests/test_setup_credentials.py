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
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import recovery             # noqa: E402

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
        fd, self.restoring = tempfile.mkstemp(prefix="hb_setup_restoring_")
        os.close(fd)

        self.writes = []
        self.threads = []
        self._saved = {k: getattr(hb, k) for k in (
            "ENV_FILE", "update_env_var", "is_setup_complete",
            "SETUP_STARTED_MARKER", "STAGING_CREDS_PATH", "RESTORING_MARKER",
            "get_factory_config")}
        self._saved_thread = hb.threading.Thread
        self._saved_limiter = hb.limiter.enabled

        hb.ENV_FILE = self.env_path
        hb.SETUP_STARTED_MARKER = self.marker
        hb.STAGING_CREDS_PATH = self.creds
        hb.RESTORING_MARKER = self.restoring
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
        for p in (self.env_path, self.marker, self.creds, self.restoring):
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


FACTORY_TUNNEL = {
    "NEWT_ID": "newt-factory",
    "NEWT_SECRET": "factory-secret-value",
    "PANGOLIN_ENDPOINT": "https://pan.example",
    "PANGOLIN_DOMAIN": "box.example.com",
}


def test_remote_setup_uses_factory_secret_when_form_sends_empty():
    """The wizard must not require re-typing a secret already in factory_config.

    JS used to block a blank secret even though /start_setup falls back to
    factory NEWT_SECRET. Empty string is what the fixed wizard sends.
    """
    h = _fresh()
    try:
        hb.get_factory_config = lambda: dict(FACTORY_TUNNEL)
        r = h.start(
            deployment_mode="remote",
            pangolin_id="newt-factory",
            pangolin_secret="",
            pangolin_endpoint="https://pan.example",
            pangolin_domain="box.example.com",
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("NEWT_SECRET") == "factory-secret-value"
        assert h.written("NEWT_ID") == "newt-factory"
    finally:
        h.close()


def test_remote_setup_form_secret_overrides_factory():
    h = _fresh()
    try:
        hb.get_factory_config = lambda: dict(FACTORY_TUNNEL)
        r = h.start(
            deployment_mode="remote",
            pangolin_id="newt-factory",
            pangolin_secret="override-secret",
            pangolin_endpoint="https://pan.example",
            pangolin_domain="box.example.com",
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("NEWT_SECRET") == "override-secret"
    finally:
        h.close()


PHRASE = "wobble tundra deputy chrome amulet salsa"
ARCHIVE = "homebrain_backup_2026-08-02.tar.gz.gpg"


def test_wizard_restore_with_master_password_seeds_it():
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("MASTER_PASSWORD") == MASTER_PW
        with open(h.creds) as f:
            data = json.load(f)
        assert data.get("recovery_adopted") is False
        assert data["password"] == MASTER_PW
    finally:
        h.close()


def test_wizard_restore_with_phrase_does_not_become_the_master_password():
    h = Harness(["MYSQL_PASSWORD="])
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": PHRASE,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("MASTER_PASSWORD") != PHRASE
        assert h.written("MASTER_PASSWORD")
        assert recovery.is_valid_new_password(h.written("MASTER_PASSWORD"))
        assert h.written("RECOVERY_BACKUP_KEY")
        with open(h.creds) as f:
            data = json.load(f)
        assert data["recovery_adopted"] is True
        assert data["recovery_phrase"] == recovery.normalize_phrase(PHRASE)
        assert data["password"] != PHRASE
    finally:
        h.close()


def test_wizard_restore_rejects_empty_and_garbage_secrets():
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": "",
        })
        assert r.status_code == 400
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": "short!",
        })
        assert r.status_code == 400
    finally:
        h.close()


def _cmd(h):
    """The shell command start_setup handed to the background task."""
    _a, kw = h.threads[-1]
    return kw["args"][1]       # run_background_task(task_name, command, log_type)


def test_wizard_restore_from_the_backup_drive_does_not_fetch_offsite():
    """The dead-box story is "I still have the drive". A local archive is
    resolved to a path here; --from-offsite would send restore.sh to rclone."""
    h = _fresh()
    saved = hb.find_backup
    hb.find_backup = lambda name: "/mnt/backup/" + name
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": PHRASE, "source": "local",
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        cmd = _cmd(h)
        assert "--from-offsite" not in cmd, cmd
        assert "/mnt/backup/" + ARCHIVE in cmd, cmd
    finally:
        hb.find_backup = saved
        h.close()


def test_wizard_restore_from_a_drive_that_does_not_have_it_is_refused():
    h = _fresh()
    saved = hb.find_backup
    hb.find_backup = lambda name: None
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": PHRASE, "source": "local",
        })
        assert r.status_code == 404, r.status_code
    finally:
        hb.find_backup = saved
        h.close()


def test_wizard_restore_defaults_to_offsite():
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        assert "--from-offsite" in _cmd(h)
    finally:
        h.close()


def test_wizard_restore_rejects_an_unknown_source():
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW, "source": "dropbox",
        })
        assert r.status_code == 400, r.status_code
    finally:
        h.close()


def test_wizard_restore_streams_into_the_log_the_wizard_shows():
    """restore.sh re-opens its own stdout onto restore.log whenever it is not
    on a TTY, so redirecting the chain was never enough: the wizard's log froze
    at deploy.sh's last line and stayed frozen for the whole restore — the
    longest and least reassuring part of a first boot. Only the env override
    puts this run's restore output where the wizard is looking."""
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        cmd = _cmd(h)
        setup_log = hb.LOG_FILES["setup"]
        assert f"RESTORE_LOG_FILE={setup_log}" in cmd, cmd
    finally:
        h.close()


def test_wizard_restore_closes_out_the_handover_after_the_restore():
    """deploy.sh must not promote the credentials on this path — it is only the
    first half — and something must promote them afterwards whatever happens,
    or a failed restore leaves the master password unreadable forever and the
    wizard stuck on a progress screen that never moves."""
    h = _fresh()
    try:
        r = h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW,
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        cmd = _cmd(h)
        assert "finish_restore $rc" in cmd, cmd
        # After the rc capture, so it runs on a failed restore too.
        assert cmd.index("rc=$?") < cmd.index("finish_restore"), cmd
        # And the chain still reports the restore's own status upwards.
        assert cmd.rstrip().endswith("exit $rc"), cmd
    finally:
        h.close()


def test_a_plain_install_has_nothing_to_finish():
    h = _fresh()
    try:
        assert h.start().status_code == 200
        cmd = _cmd(h)
        assert "finish_restore" not in cmd, cmd
        assert "RESTORE_LOG_FILE" not in cmd, cmd
    finally:
        h.close()


def test_the_handover_screen_knows_this_install_was_a_restore():
    """.restoring is the in-flight flag now and is gone before the handover
    screen renders, so the screen cannot word itself from it. The credentials
    file is the durable record of what this install was."""
    h = _fresh()
    try:
        assert h.start(deployment_mode="local", restore={
            "archive": ARCHIVE, "master_password": MASTER_PW,
        }).status_code == 200
        with open(h.creds) as f:
            assert json.load(f)["restored"] is True

        h2 = _fresh()
        try:
            assert h2.start().status_code == 200
            with open(h2.creds) as f:
                assert json.load(f)["restored"] is False
        finally:
            h2.close()
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
