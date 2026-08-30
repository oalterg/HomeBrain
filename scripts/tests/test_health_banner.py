#!/usr/bin/env python3
"""The dashboard banner can claim backups aren't scheduled after they are.

health.json is written every 30 minutes. Saving a schedule writes the systemd
timer immediately. Until the next checker run, /api/health was serving the
old "Automatic backups are not set up" line — seen on a restored box the
same afternoon the owner saved the schedule.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_health_banner.py
    pytest scripts/tests/test_health_banner.py
"""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


@contextmanager
def _health(checks, timer=False, cron=False):
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "health.json")
        with open(report, "w") as f:
            json.dump({"ts": time.time(), "overall": "warn", "checks": checks}, f)
        timer_path = os.path.join(tmp, "timer")
        cron_path = os.path.join(tmp, "cron")
        if timer:
            open(timer_path, "w").close()
        if cron:
            open(cron_path, "w").close()
        saved = (hb.HEALTH_FILE, hb.BACKUP_CRON_FILE, hb.BACKUP_TIMER_FILE,
                 hb.limiter.enabled)
        hb.HEALTH_FILE = report
        hb.BACKUP_CRON_FILE = cron_path
        hb.BACKUP_TIMER_FILE = timer_path
        hb.limiter.enabled = False
        hb.app.config["TESTING"] = True
        client = hb.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        try:
            yield client
        finally:
            (hb.HEALTH_FILE, hb.BACKUP_CRON_FILE, hb.BACKUP_TIMER_FILE,
             hb.limiter.enabled) = saved


def _backup_summaries(client):
    r = client.get("/api/health")
    assert r.status_code == 200, r.get_data(as_text=True)
    return [c["summary"] for c in r.get_json().get("checks") or []
            if c.get("id") == "backup"]


def test_stale_not_set_up_is_dropped_once_the_timer_exists():
    with _health([{"id": "backup", "level": "warn",
                   "summary": "Automatic backups are not set up"}],
                 timer=True) as client:
        assert _backup_summaries(client) == []


def test_stale_not_set_up_stays_when_there_is_still_no_schedule():
    with _health([{"id": "backup", "level": "warn",
                   "summary": "Automatic backups are not set up"}]) as client:
        assert _backup_summaries(client) == ["Automatic backups are not set up"]


def test_a_real_backup_warning_is_not_eaten():
    with _health([{"id": "backup", "level": "crit",
                   "summary": "Backup drive is not connected"}],
                 timer=True) as client:
        assert _backup_summaries(client) == ["Backup drive is not connected"]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    sys.exit(1 if failed else 0)
