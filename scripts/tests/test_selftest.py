"""Unit tests for src/selftest.py.

The property worth pinning hardest: **a check that could not run reports SKIP,
and a run containing a SKIP never summarises as OK.** Every bug this module
exists to catch was a "we did not check" being rendered as a "we checked and
it is fine".

Run:  python3 -m pytest scripts/tests/test_selftest.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import selftest  # noqa: E402
from selftest import FAIL, OK, SKIP  # noqa: E402

NOW = 1_800_000_000
FERNET = "AwBwQAEuTjD5a1TN9GHMe8tp1lJRzS3K7MV8OzsjKP8="


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    """Nothing in this file may touch Docker, the network or the disk."""
    monkeypatch.setattr(selftest, "container_id", lambda s: "")
    monkeypatch.setattr(selftest, "docker_exec", lambda *a, **k: (1, ""))
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (0, "unreachable"))
    monkeypatch.setattr(selftest, "offsite_listing", lambda: None)
    monkeypatch.setattr(selftest, "newest_archive", lambda d: None)


# --- the summary contract ---------------------------------------------------

def test_a_skip_never_summarises_as_ok():
    rows = [selftest.result("a", OK, ""), selftest.result("b", SKIP, "")]
    assert selftest.summarise(rows) == SKIP


def test_a_fail_outranks_a_skip():
    rows = [selftest.result("a", SKIP, ""), selftest.result("b", FAIL, "")]
    assert selftest.summarise(rows) == FAIL


def test_all_ok_is_ok():
    assert selftest.summarise([selftest.result("a", OK, "")]) == OK


def test_every_row_carries_a_detail(monkeypatch):
    out = selftest.run_all({}, now=NOW)
    assert out["checks"], "run_all produced no checks"
    for row in out["checks"]:
        assert row["status"] in (OK, FAIL, SKIP), row
        assert row["detail"], f"{row['name']} has no detail"


# --- password checks --------------------------------------------------------

def test_dashboard_password_rejected_is_a_failure(monkeypatch):
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (401, "nope"))
    r = selftest.check_dashboard_password({"MANAGER_PASSWORD": "x"})
    assert r["status"] == FAIL and "rejected" in r["detail"]


def test_dashboard_unreachable_is_a_skip_not_a_failure(monkeypatch):
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (0, "conn refused"))
    assert selftest.check_dashboard_password({"MANAGER_PASSWORD": "x"})["status"] == SKIP


def test_dashboard_password_accepted(monkeypatch):
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (200, "ok"))
    assert selftest.check_dashboard_password({"MANAGER_PASSWORD": "x"})["status"] == OK


def test_no_recorded_password_is_a_skip():
    assert selftest.check_dashboard_password({})["status"] == SKIP
    assert selftest.check_nextcloud_password({})["status"] == SKIP
    assert selftest.check_ha_password({})["status"] == SKIP


@pytest.fixture
def ha(monkeypatch):
    """A running Home Assistant whose owner account is called `admin`."""
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "ha_owner_username", lambda cid: "admin")


def test_ha_rejection_is_a_failure(ha, monkeypatch):
    """The #145 regression: `hass --script auth` exits 0 on "User not found",
    so for months the box reported a rotation it had never made."""
    calls = []

    def fake_http(method, url, **kw):
        calls.append(url)
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        return 200, '{"type": "invalid_auth"}'

    monkeypatch.setattr(selftest, "http", fake_http)
    r = selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})
    assert r["status"] == FAIL
    assert any("login_flow/abc" in c for c in calls), "never completed the flow"


def test_ha_acceptance(ha, monkeypatch):
    def fake_http(method, url, **kw):
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        return 200, '{"type": "create_entry"}'

    monkeypatch.setattr(selftest, "http", fake_http)
    assert selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})["status"] == OK


def test_the_owner_is_not_always_called_admin(monkeypatch):
    """Live on the production box: Home Assistant migrated from an older
    system, owner `oliaidanaberlin`, no `admin` account at all. Testing
    `admin`'s password there reports a rejection that was never even tried."""
    sent = {}

    def fake_http(method, url, data=None, **kw):
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        sent.update(json.loads(data.decode()))
        return 200, '{"type": "create_entry"}'

    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "ha_owner_username", lambda cid: "oliaidanaberlin")
    monkeypatch.setattr(selftest, "http", fake_http)
    r = selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})
    assert sent["username"] == "oliaidanaberlin"
    assert r["status"] == OK
    assert "oliaidanaberlin" in r["detail"], "the row must name the account it tested"


def test_an_unreadable_owner_is_a_skip_not_a_failure(monkeypatch):
    """'We could not tell' is not 'your password is wrong'. Guessing `admin`
    would put a red row on a box whose password is perfectly fine."""
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "ha_owner_username", lambda cid: "")
    assert selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})["status"] == SKIP


def test_ha_not_running_is_a_skip(monkeypatch):
    monkeypatch.setattr(selftest, "container_id", lambda s: "")
    assert selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})["status"] == SKIP


def test_a_self_managed_ha_password_is_not_a_failure(ha, monkeypatch):
    """Home Assistant lets its owner set their own password, and on a migrated
    box the account predates HomeBrain. "HA rejected the recorded password"
    there describes .env being out of date, not a broken login — and a red row
    demands the owner fix something that is not broken."""
    def boom(*a, **kw):
        raise AssertionError("must not test a password it does not own")

    monkeypatch.setattr(selftest, "http", boom)
    r = selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x",
                                    "HA_PASSWORD_MANAGED": "false"})
    assert r["status"] == OK
    assert "admin" in r["detail"], "the row must name the account it means"


def test_the_recorded_account_wins_over_discovery(monkeypatch):
    """Ownership is a fact HomeBrain wrote down when it knew the answer, not a
    guess re-derived per run. Discovery is the fallback for boxes provisioned
    before it was recorded — where it disagrees, the record is the truth."""
    sent = {}

    def fake_http(method, url, data=None, **kw):
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        sent.update(json.loads(data.decode()))
        return 200, '{"type": "create_entry"}'

    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "ha_owner_username", lambda cid: "someone-else")
    monkeypatch.setattr(selftest, "http", fake_http)
    selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x", "HA_ADMIN_USER": "recorded"})
    assert sent["username"] == "recorded"


def test_a_rejected_password_offers_the_repair(ha, monkeypatch):
    """A row the owner can act on carries the action. The old hint sent them to
    "Settings → Master Password" — changing every password in the house to
    correct the one service that drifted."""
    def fake_http(method, url, **kw):
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        return 200, '{"type": "invalid_auth"}'

    monkeypatch.setattr(selftest, "http", fake_http)
    r = selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})
    assert r["status"] == FAIL
    assert r["action"]["endpoint"] == "/api/ha/password/adopt"


def test_a_passing_check_carries_no_action(ha, monkeypatch):
    """Buttons are for rows that found something. A green row with a button on
    it invites the owner to change a password that is already correct."""
    def fake_http(method, url, **kw):
        if url.endswith("/auth/login_flow"):
            return 200, '{"flow_id": "abc"}'
        return 200, '{"type": "create_entry"}'

    monkeypatch.setattr(selftest, "http", fake_http)
    assert "action" not in selftest.check_ha_password({"HA_ADMIN_PASSWORD": "x"})


# --- Vault ------------------------------------------------------------------

def test_vault_disabled_is_a_skip():
    assert selftest.check_vault({"VAULT_ENABLED": "false"})["status"] == SKIP


def test_vault_container_missing_is_a_skip():
    assert selftest.check_vault({})["status"] == SKIP


def test_vault_alive_fails(monkeypatch):
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (0, "unreachable"))
    r = selftest.check_vault({})
    assert r["status"] == FAIL


def test_vault_alive_ok(monkeypatch):
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "http", lambda *a, **k: (200, "ALIVE"))
    r = selftest.check_vault({})
    assert r["status"] == OK


# --- Nextcloud data directory ----------------------------------------------

def test_nextcloud_data_dir_missing_marker_is_a_failure(monkeypatch):
    """Seen live: a populated database beside an empty data directory, with
    every container reporting healthy."""
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "docker_exec", lambda *a, **k: (
        1, "Your data directory is invalid.\nEnsure there is a file called .ncdata"))
    r = selftest.check_nextcloud_data({})
    assert r["status"] == FAIL
    assert ".ncdata" in r["hint"]
    assert "backups" in r["hint"].lower()


def test_nextcloud_data_dir_healthy(monkeypatch):
    monkeypatch.setattr(selftest, "container_id", lambda s: "cid")
    monkeypatch.setattr(selftest, "docker_exec", lambda *a, **k: (0, "  - installed: true"))
    assert selftest.check_nextcloud_data({})["status"] == OK


def test_nextcloud_not_running_is_a_skip():
    assert selftest.check_nextcloud_data({})["status"] == SKIP


# --- backups ----------------------------------------------------------------

@pytest.fixture
def drive(monkeypatch):
    """A backup drive that is really mounted. Most backup checks are about the
    archives, not the drive, so they say so instead of leaning on the host."""
    monkeypatch.setattr(selftest, "is_mountpoint", lambda d: True)


def test_no_local_backup_is_a_failure(drive):
    assert selftest.check_local_backup({}, NOW, "/mnt/backup")["status"] == FAIL


def test_fresh_local_backup(drive, monkeypatch):
    monkeypatch.setattr(selftest, "newest_archive",
                        lambda d: ("homebrain_backup.tar.gz.gpg", NOW - selftest.DAY))
    assert selftest.check_local_backup({}, NOW, "/mnt/backup")["status"] == OK


def test_stale_local_backup(drive, monkeypatch):
    monkeypatch.setattr(selftest, "newest_archive",
                        lambda d: ("homebrain_backup.tar.gz.gpg", NOW - 5 * selftest.DAY))
    assert selftest.check_local_backup({}, NOW, "/mnt/backup")["status"] == FAIL


def test_a_weekly_schedule_tolerates_a_week_old_backup(drive, monkeypatch):
    monkeypatch.setattr(selftest, "newest_archive",
                        lambda d: ("homebrain_backup.tar.gz.gpg", NOW - 5 * selftest.DAY))
    env = {"BACKUP_DAY_WEEK": "0"}
    assert selftest.check_local_backup(env, NOW, "/mnt/backup")["status"] == OK


def test_recent_archives_on_a_drive_that_is_gone_are_a_failure(monkeypatch):
    """The one a fresh timestamp cannot catch. `nofail` in fstab means `mount`
    exits 0 with the drive absent, so backups keep succeeding onto the root
    disk — newest archive an hour old, and not one of them on the drive."""
    monkeypatch.setattr(selftest, "is_mountpoint", lambda d: False)
    monkeypatch.setattr(selftest, "newest_archive",
                        lambda d: ("homebrain_backup.tar.gz.gpg", NOW - 3600))
    r = selftest.check_local_backup({}, NOW, "/mnt/backup")
    assert r["status"] == FAIL
    assert "internal disk" in r["detail"]


def test_no_drive_mode_does_not_want_a_mountpoint(monkeypatch):
    """Internal storage is a directory on the root disk by definition, so the
    drive check must not fire there."""
    monkeypatch.setattr(selftest, "is_mountpoint", lambda d: False)
    monkeypatch.setattr(selftest, "newest_archive",
                        lambda d: ("homebrain_backup.tar.gz.gpg", NOW - selftest.DAY))
    env = {"BACKUP_INTERNAL": "true"}
    assert selftest.check_local_backup(env, NOW, "/var/backups/homebrain")["status"] == OK


def test_offsite_disabled_is_a_skip_that_says_why():
    r = selftest.check_offsite({}, NOW)
    assert r["status"] == SKIP
    assert "fire or theft" in r["hint"]


def test_offsite_unreadable_is_a_failure():
    assert selftest.check_offsite({"OFFSITE_ENABLED": "true"}, NOW)["status"] == FAIL


def test_offsite_empty_is_a_failure(monkeypatch):
    monkeypatch.setattr(selftest, "offsite_listing", lambda: [])
    assert selftest.check_offsite({"OFFSITE_ENABLED": "true"}, NOW)["status"] == FAIL


def test_offsite_with_archives(monkeypatch):
    monkeypatch.setattr(selftest, "offsite_listing",
                        lambda: [{"Name": "a.tar.gz.gpg", "ModTime": "2026-08-02T22:50:53Z"}])
    r = selftest.check_offsite({"OFFSITE_ENABLED": "true"}, NOW)
    assert r["status"] == OK and "2026-08-02" in r["detail"]


# --- instance secrets -------------------------------------------------------

def test_truncated_fernet_key_is_a_failure():
    """The #147 bug, now visible to the owner instead of surfacing as a 401
    three layers away."""
    r = selftest.check_instance_secrets({"HOMEBRAIN_EMAIL_KEY": FERNET.rstrip("=")})
    assert r["status"] == FAIL and "43 characters" in r["detail"]


def test_intact_fernet_key():
    assert selftest.check_instance_secrets({"HOMEBRAIN_EMAIL_KEY": FERNET})["status"] == OK


def test_no_key_yet_is_a_skip():
    assert selftest.check_instance_secrets({})["status"] == SKIP


# --- remote access and version ---------------------------------------------

def test_local_only_box_skips_remote_access():
    assert selftest.check_remote_access({"DEPLOYMENT_MODE": "local"})["status"] == SKIP


def test_remote_box_that_does_not_answer_is_a_failure():
    env = {"DEPLOYMENT_MODE": "remote", "PANGOLIN_DOMAIN": "x.example.com"}
    assert selftest.check_remote_access(env)["status"] == FAIL


def test_version_behind_is_a_failure():
    assert selftest.check_version("v2026.07.29", "v2026.08.01")["status"] == FAIL


def test_version_current():
    assert selftest.check_version("v2026.08.01", "v2026.08.01")["status"] == OK


def test_github_unreachable_is_a_skip_not_a_pass():
    assert selftest.check_version("v2026.08.01", "")["status"] == SKIP


def test_an_untagged_box_is_a_skip_not_a_failure():
    """get_local_version() returns the literal "unknown" for a dev checkout.
    Reporting that as "you are behind" is the same lie in the other direction."""
    r = selftest.check_version("unknown", "v2026.08.01")
    assert r["status"] == SKIP and "not running a tagged release" in r["hint"]
