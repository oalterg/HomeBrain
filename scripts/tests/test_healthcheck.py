"""Unit tests for the pure logic in scripts/healthcheck.py.

Run:  python3 -m pytest scripts/tests/test_healthcheck.py
"""
import datetime
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import healthcheck  # noqa: E402
from healthcheck import (  # noqa: E402
    DAY,
    backup_log_outcome,
    check_offsite,
    check_reboot,
    check_update,
    compose_message,
    daily_note_date,
    daily_retention_days,
    decide_notification,
    disk_level,
    expected_backup_interval,
    parse_env,
    release_key,
    sweep_openclaw_daily_memory,
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


def _offsite(env, state=None, now=NOW, syncing=False):
    """Run check_offsite against a temp state file (or a missing one).

    `syncing=True` writes a run-file naming a live PID — this test process —
    which is exactly what common.sh:offsite_mirror publishes for the duration
    of a mirror.
    """
    orig_state = healthcheck.OFFSITE_STATE
    orig_run = healthcheck.OFFSITE_RUN_FILE
    with tempfile.TemporaryDirectory() as d:
        run_path = os.path.join(d, "offsite.running")
        if syncing:
            with open(run_path, "w") as f:
                f.write(f"{os.getpid()}\n")
        healthcheck.OFFSITE_RUN_FILE = run_path
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
            healthcheck.OFFSITE_STATE = orig_state
            healthcheck.OFFSITE_RUN_FILE = orig_run


def _run_file(contents):
    """Point OFFSITE_RUN_FILE at a file holding `contents`."""
    orig = healthcheck.OFFSITE_RUN_FILE
    d = tempfile.TemporaryDirectory()
    path = os.path.join(d.name, "offsite.running")
    with open(path, "w") as f:
        f.write(contents)
    healthcheck.OFFSITE_RUN_FILE = path
    return orig, d


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


# offsite_is_syncing() proves a PID is alive by looking for /proc/<pid>, which
# is correct on the box and absent on a developer's Mac. Skip rather than fail:
# four red tests that mean "wrong OS" teach people to ignore red tests.
needs_proc = pytest.mark.skipif(not os.path.isdir("/proc"),
                                reason="offsite_is_syncing() reads /proc; Linux only")


@needs_proc
def test_offsite_in_progress_outranks_a_previous_failure():
    """A running mirror is the truth; the state file describes the LAST run.

    Without this the hours-long upload of a multi-GB archive reports "the last
    off-site copy failed" for its whole duration.
    """
    env = {"OFFSITE_ENABLED": "true"}
    assert _offsite(env, {"ts": NOW, "ok": False})["level"] == "warn"
    running = _offsite(env, {"ts": NOW, "ok": False}, syncing=True)
    assert running["level"] == "ok" and "in progress" in running["summary"]


@needs_proc
def test_offsite_in_progress_outranks_staleness():
    env = {"OFFSITE_ENABLED": "true"}
    assert _offsite(env, {"ts": NOW - 3 * DAY, "ok": True})["level"] == "warn"
    running = _offsite(env, {"ts": NOW - 3 * DAY, "ok": True}, syncing=True)
    assert running["level"] == "ok" and "in progress" in running["summary"]


@needs_proc
def test_offsite_first_ever_copy_in_progress_is_not_a_warning():
    """No state file yet, because the first mirror has not finished one."""
    running = _offsite({"OFFSITE_ENABLED": "true"}, syncing=True)
    assert running["level"] == "ok" and "First off-site copy" in running["summary"]


def test_offsite_still_silent_when_disabled_even_while_syncing():
    """OFFSITE_ENABLED=false outranks everything — the backfill era ran mirrors
    with the flag still off, and health must stay quiet about them."""
    assert _offsite({"OFFSITE_ENABLED": "false"}, syncing=True) is None


@needs_proc
def test_offsite_is_syncing_reads_a_live_pid():
    orig, d = _run_file(f"{os.getpid()}\n")
    try:
        assert healthcheck.offsite_is_syncing() is True
    finally:
        healthcheck.OFFSITE_RUN_FILE = orig
        d.cleanup()


def test_offsite_run_file_with_a_dead_pid_is_not_syncing():
    """A run-file outliving its mirror must not mask a real failure. /var/run
    is tmpfs so a reboot clears it, but a SIGKILL leaves it behind. 4194305 is
    one above Linux's PID_MAX_LIMIT, so it can never name a live process."""
    orig, d = _run_file("4194305\n")
    try:
        assert healthcheck.offsite_is_syncing() is False
    finally:
        healthcheck.OFFSITE_RUN_FILE = orig
        d.cleanup()


def test_offsite_garbage_run_file_is_not_syncing():
    orig, d = _run_file("not-a-pid\n")
    try:
        assert healthcheck.offsite_is_syncing() is False
    finally:
        healthcheck.OFFSITE_RUN_FILE = orig
        d.cleanup()


def test_offsite_missing_run_file_is_not_syncing():
    orig = healthcheck.OFFSITE_RUN_FILE
    try:
        healthcheck.OFFSITE_RUN_FILE = "/nonexistent/offsite.running"
        assert healthcheck.offsite_is_syncing() is False
    finally:
        healthcheck.OFFSITE_RUN_FILE = orig


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


# --- files drive ------------------------------------------------------------
# The drive holding the user's photos is either mounted or Nextcloud is down.
# The regression: with it unplugged every container still reported "running",
# the container check said "Containers healthy", and nothing told the owner.

def test_files_drive_silent_when_on_internal_disk():
    assert healthcheck.check_files_drive({}) is None
    assert healthcheck.check_files_drive(
        {"NEXTCLOUD_DATA_DIR": healthcheck.NC_DATA_DEFAULT}) is None


def test_files_drive_missing_is_critical():
    out = healthcheck.check_files_drive({"NEXTCLOUD_DATA_DIR": "/mnt/nextcloud-data"})
    assert out["level"] == "crit"
    assert "not connected" in out["summary"]


def test_files_drive_mounted_reports_its_own_usage(tmp_path, monkeypatch):
    path = str(tmp_path)
    monkeypatch.setattr(healthcheck.os.path, "ismount", lambda p: p == path)
    out = healthcheck.check_files_drive({"NEXTCLOUD_DATA_DIR": path})
    assert out["id"] == "disk_files"
    assert "Files drive" in out["summary"]


# --- email fallback ---------------------------------------------------------
# A notification system with one channel the owner can silently uninstall is
# not one. These pin the fallback semantics: email fires only when the push did
# not go, and a wrong Fernet key is a refusal to send rather than an SMTP login
# with a ciphertext for a password.

def _email_env(**over):
    env = {"NOTIFY_EMAIL": "owner@example.com", "HOMEBRAIN_EMAIL_KEY": "k"}
    env.update(over)
    return env


def test_no_recipient_means_no_email_target(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "EMAIL_ACCOUNTS_FILE", str(tmp_path / "none.json"))
    assert healthcheck.resolve_email_target({}) is None


def test_no_account_means_no_email_target(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "EMAIL_ACCOUNTS_FILE", str(tmp_path / "none.json"))
    assert healthcheck.resolve_email_target(_email_env()) is None


def test_cloud_email_is_the_fallback_recipient(tmp_path, monkeypatch):
    p = tmp_path / "email_accounts.json"
    p.write_text(json.dumps({"accounts": [
        {"user": "box@example.com", "smtp_host": "smtp.example.com"}]}))
    monkeypatch.setattr(healthcheck, "EMAIL_ACCOUNTS_FILE", str(p))
    account, to = healthcheck.resolve_email_target({"CLOUD_EMAIL": "owner@example.com"})
    assert to == "owner@example.com"
    assert account["smtp_host"] == "smtp.example.com"


def test_notify_email_overrides_cloud_email(tmp_path, monkeypatch):
    p = tmp_path / "email_accounts.json"
    p.write_text(json.dumps({"accounts": [
        {"user": "box@example.com", "smtp_host": "smtp.example.com"}]}))
    monkeypatch.setattr(healthcheck, "EMAIL_ACCOUNTS_FILE", str(p))
    _, to = healthcheck.resolve_email_target(
        {"CLOUD_EMAIL": "old@example.com", "NOTIFY_EMAIL": "new@example.com"})
    assert to == "new@example.com"


def test_an_account_without_smtp_is_not_usable(tmp_path, monkeypatch):
    p = tmp_path / "email_accounts.json"
    p.write_text(json.dumps({"accounts": [{"user": "box@example.com"}]}))
    monkeypatch.setattr(healthcheck, "EMAIL_ACCOUNTS_FILE", str(p))
    assert healthcheck.resolve_email_target(_email_env()) is None


def test_undecryptable_password_refuses_to_send(monkeypatch):
    """decrypt_secret returns "" rather than ciphertext on a wrong key. Sending
    that as a password fails at the SMTP server with nothing pointing at the
    real cause, so refuse and say so."""
    sent = []
    monkeypatch.setattr(healthcheck, "log", lambda m: sent.append(m))
    account = {"user": "box@example.com", "smtp_host": "smtp.example.com",
               "smtp_password": "gAAAAAnot-decryptable"}
    ok = healthcheck.send_email(_email_env(HOMEBRAIN_EMAIL_KEY="wrong"),
                                account, "owner@example.com", "s", "t")
    assert ok is False
    assert any("could not be decrypted" in m for m in sent)


def test_subject_reflects_severity():
    crit = [{"level": "crit", "summary": "x"}]
    warn = [{"level": "warn", "summary": "x"}]
    assert healthcheck.email_subject(crit, []) == "HomeBrain needs attention"
    assert healthcheck.email_subject(warn, []) == "HomeBrain: something to look at"
    assert healthcheck.email_subject([], [{"summary": "y"}]) == "HomeBrain: resolved"


# --- dead-man's switch ------------------------------------------------------
# The switch must be OFF until the Worker half exists: a heartbeat posted into
# the void is worse than none, because the owner believes they are covered.

def test_heartbeat_is_off_by_default():
    assert healthcheck.heartbeat_url({}, {}) == ""


def test_heartbeat_stays_off_without_a_registrar():
    assert healthcheck.heartbeat_url({"HEARTBEAT_ENABLED": "true"}, {}) == ""


def test_heartbeat_derives_the_url_from_the_registrar():
    url = healthcheck.heartbeat_url(
        {"HEARTBEAT_ENABLED": "true"},
        {"REGISTRAR_URL": "https://reg.example.com/"})
    assert url == "https://reg.example.com/heartbeat"


def test_an_explicit_url_wins_and_needs_no_toggle():
    url = healthcheck.heartbeat_url({"HEARTBEAT_URL": "https://x/hb"},
                                    {"REGISTRAR_URL": "https://reg.example.com"})
    assert url == "https://x/hb"


def test_no_secret_means_no_heartbeat(monkeypatch):
    logged = []
    monkeypatch.setattr(healthcheck, "log", lambda m: logged.append(m))
    out = healthcheck.send_heartbeat({"HEARTBEAT_URL": "https://x/hb"}, {}, "ok", NOW)
    assert out is False
    assert any("REGISTRAR_SECRET" in m for m in logged)


def test_unconfigured_heartbeat_returns_none_not_false():
    """None means 'not armed' and stays silent; False means 'armed and it
    failed' and gets logged. Collapsing them would either spam an unconfigured
    box or hide a broken switch."""
    assert healthcheck.send_heartbeat({}, {}, "ok", NOW) is None


def test_heartbeat_posts_device_id_and_health(monkeypatch):
    seen = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)
    out = healthcheck.send_heartbeat(
        {"HEARTBEAT_URL": "https://x/hb"},
        {"REGISTRAR_SECRET": "s3cret", "NEWT_ID": "newt-abc"}, "warn", NOW)
    assert out is True
    assert seen["url"] == "https://x/hb"
    assert seen["auth"] == "Bearer s3cret"
    assert seen["body"]["device_id"] == "newt-abc"
    assert seen["body"]["overall"] == "warn"


def test_a_failing_heartbeat_is_logged_not_raised(monkeypatch):
    logged = []
    monkeypatch.setattr(healthcheck, "log", lambda m: logged.append(m))
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    out = healthcheck.send_heartbeat({"HEARTBEAT_URL": "https://x/hb"},
                                     {"REGISTRAR_SECRET": "s"}, "ok", NOW)
    assert out is False
    assert any("heartbeat failed" in m for m in logged)


def test_daily_retention_days():
    assert daily_retention_days({}) == 30
    assert daily_retention_days({"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "0"}) == 0
    assert daily_retention_days({"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "7"}) == 7
    assert daily_retention_days({"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "nope"}) == 30
    assert daily_retention_days({"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "-1"}) == 30


def test_daily_note_date_only_plain_dated_files():
    assert daily_note_date("2026-07-01.md") == datetime.date(2026, 7, 1)
    assert daily_note_date("2026-07-01-washer.md") is None
    assert daily_note_date("MEMORY.md") is None
    assert daily_note_date("2026-13-40.md") is None


def test_memory_sweep_removes_old_keeps_recent(tmp_path):
    today = datetime.date(2026, 8, 19)
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "2026-07-01.md").write_text("old")
    (mem / "2026-08-18.md").write_text("yesterday")
    (mem / "2026-08-19.md").write_text("today")
    (mem / "2026-07-01-slug.md").write_text("slugged")
    (mem / ".dreams").mkdir()
    (tmp_path / "MEMORY.md").write_text("pinned")
    logged = []
    removed = sweep_openclaw_daily_memory({}, str(mem), today, log_fn=logged.append)
    assert any(p.endswith("2026-07-01.md") for p in removed)
    assert not (mem / "2026-07-01.md").exists()
    assert (mem / "2026-08-18.md").exists()
    assert (mem / "2026-08-19.md").exists()
    assert (mem / "2026-07-01-slug.md").exists()
    assert (mem / ".dreams").is_dir()
    assert (tmp_path / "MEMORY.md").read_text() == "pinned"


def test_memory_sweep_missing_dir_is_noop(tmp_path):
    assert sweep_openclaw_daily_memory({}, str(tmp_path / "nope"), datetime.date.today()) == []


def test_memory_sweep_zero_retention_is_noop(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "2020-01-01.md").write_text("ancient")
    removed = sweep_openclaw_daily_memory(
        {"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "0"},
        str(mem), datetime.date(2026, 8, 19))
    assert removed == []
    assert (mem / "2020-01-01.md").exists()


def test_memory_sweep_warns_on_large_memory_md_even_when_sweep_disabled(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (tmp_path / "MEMORY.md").write_bytes(b"x" * (healthcheck.MEMORY_MD_WARN_BYTES + 1))
    logged = []
    sweep_openclaw_daily_memory(
        {"OPENCLAW_MEMORY_DAILY_RETENTION_DAYS": "0"},
        str(mem), datetime.date(2026, 8, 19), log_fn=logged.append)
    assert any("MEMORY.md is" in m for m in logged)


def test_memory_sweep_warns_on_large_memory_md(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (tmp_path / "MEMORY.md").write_bytes(b"x" * (healthcheck.MEMORY_MD_WARN_BYTES + 1))
    logged = []
    sweep_openclaw_daily_memory({}, str(mem), datetime.date(2026, 8, 19), log_fn=logged.append)
    assert any("MEMORY.md is" in m for m in logged)


def test_memory_sweep_retention_is_keep_last_n_days(tmp_path):
    today = datetime.date(2026, 8, 19)
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "2026-07-20.md").write_text("exactly 30 days")
    (mem / "2026-07-21.md").write_text("29 days")
    (mem / "2026-08-20.md").write_text("tomorrow")
    sweep_openclaw_daily_memory({}, str(mem), today)
    assert not (mem / "2026-07-20.md").exists()
    assert (mem / "2026-07-21.md").exists()
    assert (mem / "2026-08-20.md").exists()

