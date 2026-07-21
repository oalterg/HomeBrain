"""Unit tests for the pure logic in scripts/healthcheck.py.

Run:  python3 -m pytest scripts/tests/test_healthcheck.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import healthcheck  # noqa: E402
from healthcheck import (  # noqa: E402
    DAY,
    backup_log_outcome,
    check_offsite,
    check_reboot,
    check_update,
    compose_message,
    decide_notification,
    disk_level,
    expected_backup_interval,
    parse_env,
    release_key,
)

NOW = 1_800_000_000


def test_parse_env_strips_quotes_and_comments():
    env = parse_env(
        "# comment\n"
        "MASTER_PASSWORD='p4ss'\n"
        'BACKUP_HOUR="3"\n'
        "BACKUP_RETENTION=8\n"
        "EMPTY=\n"
    )
    assert env["MASTER_PASSWORD"] == "p4ss"
    assert env["BACKUP_HOUR"] == "3"
    assert env["BACKUP_RETENTION"] == "8"
    assert env["EMPTY"] == ""


def test_expected_backup_interval():
    assert expected_backup_interval({}) == DAY
    assert expected_backup_interval({"BACKUP_DAY_WEEK": "*"}) == DAY
    assert expected_backup_interval({"BACKUP_DAY_WEEK": "0"}) == 7 * DAY
    assert expected_backup_interval({"BACKUP_DAY_MONTH": "1"}) == 31 * DAY
    # day-of-month wins over day-of-week (matches cron semantics closely enough)
    assert expected_backup_interval(
        {"BACKUP_DAY_MONTH": "1", "BACKUP_DAY_WEEK": "0"}) == 31 * DAY


def test_backup_log_outcome():
    assert backup_log_outcome("") == "none"
    assert backup_log_outcome("junk\n") == "none"
    ok = "=== Starting Backup [x]: date ===\nstuff\n=== Backup Complete: /mnt/backup/a.tar.gz ===\n"
    assert backup_log_outcome(ok) == "complete"
    failed = ok + "=== Starting Backup [x]: date ===\n[ERROR] died\n"
    assert backup_log_outcome(failed) == "started"


def test_disk_level_thresholds():
    assert disk_level(0) == "ok"
    assert disk_level(84) == "ok"
    assert disk_level(85) == "warn"
    assert disk_level(94) == "warn"
    assert disk_level(95) == "crit"
    assert disk_level(100) == "crit"


def test_notify_on_escalation_only():
    # First sighting of ok: silence.
    assert decide_notification(None, "ok", NOW) is None
    # First sighting of a problem: alert.
    assert decide_notification(None, "warn", NOW) == "alert"
    assert decide_notification(None, "crit", NOW) == "alert"
    # Same level again, recently notified: silence.
    prev = {"level": "warn", "last_notified": NOW - 3600}
    assert decide_notification(prev, "warn", NOW) is None
    # Escalation warn -> crit: alert even if recently notified.
    assert decide_notification(prev, "crit", NOW) == "alert"
    # De-escalation crit -> warn: no new alert.
    prev = {"level": "crit", "last_notified": NOW - 3600}
    assert decide_notification(prev, "warn", NOW) is None


def test_notify_recovery_only_from_crit():
    assert decide_notification({"level": "crit", "last_notified": NOW}, "ok", NOW) == "recovery"
    assert decide_notification({"level": "warn", "last_notified": NOW}, "ok", NOW) is None


def test_steady_state_reminders():
    # crit re-notifies after 24h
    prev = {"level": "crit", "last_notified": NOW - DAY - 1}
    assert decide_notification(prev, "crit", NOW) == "alert"
    prev = {"level": "crit", "last_notified": NOW - DAY + 3600}
    assert decide_notification(prev, "crit", NOW) is None
    # warn re-notifies after 7d
    prev = {"level": "warn", "last_notified": NOW - 7 * DAY - 1}
    assert decide_notification(prev, "warn", NOW) == "alert"
    prev = {"level": "warn", "last_notified": NOW - 6 * DAY}
    assert decide_notification(prev, "warn", NOW) is None


def test_info_level_alerts_once_then_weekly():
    # ok -> info (e.g. "update available") pushes once...
    prev = {"level": "ok", "last_notified": 0}
    assert decide_notification(prev, "info", NOW) == "alert"
    # ...then stays quiet inside the 7-day reminder window...
    prev = {"level": "info", "last_notified": NOW - DAY}
    assert decide_notification(prev, "info", NOW) is None
    # ...and info -> ok produces no recovery noise.
    assert decide_notification({"level": "info", "last_notified": NOW}, "ok", NOW) is None


def _offsite(env, state=None, now=NOW):
    """Run check_offsite against a temp state file (or a missing one)."""
    orig = healthcheck.OFFSITE_STATE
    try:
        if state is None:
            healthcheck.OFFSITE_STATE = "/nonexistent/offsite.json"
            return check_offsite(env, now)
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump(state, f)
            f.flush()
            healthcheck.OFFSITE_STATE = f.name
            return check_offsite(env, now)
    finally:
        healthcheck.OFFSITE_STATE = orig


def test_offsite_disabled_is_silent():
    assert _offsite({}) is None
    assert _offsite({"OFFSITE_ENABLED": "false"}) is None


def test_offsite_enabled_but_never_ran():
    c = _offsite({"OFFSITE_ENABLED": "true"})
    assert c["level"] == "warn" and "has not run" in c["summary"]


def test_offsite_last_run_failed():
    c = _offsite({"OFFSITE_ENABLED": "true"}, {"ts": NOW, "ok": False})
    assert c["level"] == "warn" and "failed" in c["summary"]
    # warn-only by design: local backups are the data protection
    assert "local backups are unaffected" in c["summary"]


def test_offsite_stale_and_fresh():
    env = {"OFFSITE_ENABLED": "true"}  # daily schedule -> stale after 2 days
    ok = _offsite(env, {"ts": NOW - DAY, "ok": True})
    assert ok["level"] == "ok"
    stale = _offsite(env, {"ts": NOW - 3 * DAY, "ok": True})
    assert stale["level"] == "warn" and "3 days" in stale["summary"]
    # a weekly schedule tolerates a week-old copy
    weekly = _offsite({"OFFSITE_ENABLED": "true", "BACKUP_DAY_WEEK": "0"},
                      {"ts": NOW - 6 * DAY, "ok": True})
    assert weekly["level"] == "ok"


def test_reboot_not_pending_is_ok():
    with tempfile.TemporaryDirectory() as d:
        healthcheck.REBOOT_REQUIRED_FILE = os.path.join(d, "reboot-required")
        c = check_reboot()
        assert c["level"] == "ok"


def test_reboot_pending_warns_with_packages():
    with tempfile.TemporaryDirectory() as d:
        marker = os.path.join(d, "reboot-required")
        healthcheck.REBOOT_REQUIRED_FILE = marker
        open(marker, "w").close()
        c = check_reboot()
        assert c["level"] == "warn" and "Restart" in c["summary"]
        # duplicate package lines collapse; detail lists them
        with open(marker + ".pkgs", "w") as f:
            f.write("linux-image-generic\nlibc6\nlinux-image-generic\n")
        c = check_reboot()
        assert c["level"] == "warn"
        assert "libc6" in c["summary"] and c["summary"].count("linux-image") == 1


def test_compose_message():
    alerts = [{"level": "crit", "summary": "Backup drive is not connected"},
              {"level": "warn", "summary": "System disk is 91% full"}]
    msg = compose_message(alerts, [])
    assert msg.startswith("🚨 HomeBrain needs attention:")
    assert "• Backup drive is not connected" in msg
    assert "dashboard" in msg
    rec = compose_message([], [{"level": "ok", "summary": "Backups are up to date"}])
    assert rec.startswith("✅ Resolved: Backups are up to date")


# --- update check ----------------------------------------------------------

def _stable(tmpdir, ref):
    """Point healthcheck at a version.json pinned to `ref` on stable."""
    path = os.path.join(tmpdir, "version.json")
    with open(path, "w") as f:
        json.dump({"channel": "stable", "ref": ref}, f)
    healthcheck.VERSION_FILE = path


def test_release_key_orders_date_and_semver_tags():
    assert release_key("v2026.07.21") == (2026, 7, 21)
    assert release_key("v1.1.0") == (1, 1, 0)
    assert release_key("v0.1") == (0, 1)
    assert release_key("v0.1") < release_key("v1.0.0") < release_key("v2026.06.12")
    assert release_key("v2026.07.19") < release_key("v2026.07.21")
    # unorderable input must be reported as such, not guessed at
    assert release_key("main") is None
    assert release_key("") is None
    assert release_key(None) is None


def test_update_available_only_when_release_is_newer(tmp_path):
    _stable(str(tmp_path), "v2026.07.19")
    state = {"update": {"ts": NOW, "latest": "v2026.07.21",
                        "installed": "v2026.07.19"}}
    out = check_update(state, NOW)
    assert out["level"] == "info"
    assert "v2026.07.21" in out["summary"]


def test_older_release_is_not_an_update(tmp_path):
    """The bug: a cached tag older than what's installed read as `!=` and so
    advertised a downgrade the update script would refuse."""
    _stable(str(tmp_path), "v2026.07.21")
    state = {"update": {"ts": NOW, "latest": "v2026.07.19",
                        "installed": "v2026.07.21"}}
    out = check_update(state, NOW)
    assert out["level"] == "ok"
    assert out["summary"] == "HomeBrain is up to date"


def test_same_release_is_up_to_date(tmp_path):
    _stable(str(tmp_path), "v2026.07.21")
    state = {"update": {"ts": NOW, "latest": "v2026.07.21",
                        "installed": "v2026.07.21"}}
    assert check_update(state, NOW)["level"] == "ok"


def test_cache_is_invalidated_when_installed_version_changes(tmp_path, monkeypatch):
    """A cached `latest` is only valid for the ref it was compared against;
    after an update the probe must run again rather than reuse it."""
    _stable(str(tmp_path), "v2026.07.21")
    state = {"update": {"ts": NOW, "latest": "v2026.07.19",
                        "installed": "v2026.07.19"}}   # cached pre-update

    calls = []

    class _Resp:
        def read(self):
            return b'{"tag_name": "v2026.07.21"}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return _Resp()

    # Patch only the transport — json.load is shared with the version.json
    # read at the top of check_update, so stubbing it would break that too.
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)

    out = check_update(state, NOW)
    assert calls, "stale cache should have forced a fresh probe"
    assert out["level"] == "ok"
    assert state["update"]["installed"] == "v2026.07.21"


def test_unorderable_tag_stays_quiet(tmp_path):
    """Never nag toward a tag we cannot order against the installed one."""
    _stable(str(tmp_path), "v2026.07.21")
    state = {"update": {"ts": NOW, "latest": "nightly",
                        "installed": "v2026.07.21"}}
    assert check_update(state, NOW)["level"] == "ok"


def test_beta_channel_is_skipped(tmp_path):
    path = os.path.join(str(tmp_path), "version.json")
    with open(path, "w") as f:
        json.dump({"channel": "beta", "ref": "main"}, f)
    healthcheck.VERSION_FILE = path
    assert check_update({}, NOW) is None
