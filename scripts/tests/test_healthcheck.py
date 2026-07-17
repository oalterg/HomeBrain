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
    compose_message,
    decide_notification,
    disk_level,
    expected_backup_interval,
    parse_env,
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


def test_compose_message():
    alerts = [{"level": "crit", "summary": "Backup drive is not connected"},
              {"level": "warn", "summary": "System disk is 91% full"}]
    msg = compose_message(alerts, [])
    assert msg.startswith("🚨 HomeBrain needs attention:")
    assert "• Backup drive is not connected" in msg
    assert "dashboard" in msg
    rec = compose_message([], [{"level": "ok", "summary": "Backups are up to date"}])
    assert rec.startswith("✅ Resolved: Backups are up to date")
