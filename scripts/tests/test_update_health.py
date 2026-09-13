#!/usr/bin/env python3
"""OS upgrade command shape, update-offer API, reboot scheduling.

The investigation in docs/plans/UPDATE_AND_HEALTH.md: the OS button used to
report success after a failed apt-get, offer older tags as updates, and have
no reboot. These tests pin the replacements.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_update_health.py
    pytest scripts/tests/test_update_health.py
"""
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@contextmanager
def _client(version=None):
    tmp = tempfile.TemporaryDirectory()
    status = os.path.join(tmp.name, "status.json")
    ver = os.path.join(tmp.name, "version.json")
    if version is not None:
        with open(ver, "w") as f:
            json.dump(version, f)
    saved = (hb.STATUS_FILE, hb.VERSION_FILE, hb.limiter.enabled,
             dict(hb.current_task_status))
    hb.STATUS_FILE = status
    hb.VERSION_FILE = ver
    hb.limiter.enabled = False
    hb.current_task_status.update({"status": "idle", "message": "", "log_type": "setup"})
    hb.app.config["TESTING"] = True
    client = hb.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    try:
        yield client
    finally:
        hb.STATUS_FILE, hb.VERSION_FILE, hb.limiter.enabled = saved[:3]
        hb.current_task_status.clear()
        hb.current_task_status.update(saved[3])
        tmp.cleanup()


def test_os_upgrade_command_matches_nightly_and_fails_closed():
    cmd = hb.os_upgrade_command("/tmp/homebrain-os.log")
    assert "unattended-upgrade" in cmd
    assert "apt-get upgrade" not in cmd
    assert "docker compose" not in cmd
    assert "remove-orphans" not in cmd
    assert cmd.count("&&") >= 4
    assert "Upgrade complete" in cmd.split("&&")[-1]


def test_check_update_does_not_offer_an_older_stable(monkeypatch):
    monkeypatch.setattr(
        hb.requests, "get",
        lambda *a, **k: _Resp(200, {"tag_name": "v2026.07.19"}))
    with _client({"channel": "stable", "ref": "v2026.07.21"}) as client:
        data = client.get("/api/manager/check_update?channel=stable").get_json()
        assert data["available"] is False
        assert "Up to date" in data["message"]


def test_check_update_offers_a_newer_stable(monkeypatch):
    monkeypatch.setattr(
        hb.requests, "get",
        lambda *a, **k: _Resp(200, {"tag_name": "v2026.07.21"}))
    with _client({"channel": "stable", "ref": "v2026.07.19"}) as client:
        data = client.get("/api/manager/check_update?channel=stable").get_json()
        assert data["available"] is True
        assert data["target_ref"] == "v2026.07.21"


def test_upgrade_409_when_a_task_is_running():
    with _client() as client:
        hb.write_status({"status": "running", "message": "busy", "log_type": "setup"})
        r = client.post("/api/upgrade")
        assert r.status_code == 409


def test_reboot_is_scheduled_not_inline(monkeypatch):
    started = []

    class _T:
        def __init__(self, target=None, daemon=None, args=()):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(hb.threading, "Thread", _T)
    with _client() as client:
        r = client.post("/api/system/reboot")
        assert r.status_code == 200
        assert r.get_json()["status"] == "started"
        assert started, "reboot must be scheduled on a thread"
        assert hb.read_status()["status"] == "running"


def test_claim_task_only_one_job_holds_the_slot():
    with _client():
        assert hb.claim_task(
            {"status": "running", "message": "update", "log_type": "update"})
        assert hb.claim_task(
            {"status": "running", "message": "backup", "log_type": "backup"}) is False
        assert hb.read_status()["log_type"] == "update"


def test_claim_task_is_atomic_under_contention():
    with _client():
        won = []

        def attempt(i):
            if hb.claim_task({"status": "running", "message": str(i),
                              "log_type": "setup"}):
                won.append(i)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(won) == 1, won


def test_backup_409s_on_shared_running_status_even_if_worker_local_is_idle(monkeypatch):
    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise AssertionError("backup must not start")

    monkeypatch.setattr(hb.threading, "Thread", _T)
    with _client() as client:
        hb.write_status({"status": "running", "message": "Updating",
                         "log_type": "update"})
        hb.current_task_status.update(
            {"status": "idle", "message": "", "log_type": "setup"})
        r = client.post("/api/backup/now", json={"strategy": "full"})
        assert r.status_code == 409
        r = client.post("/api/restore", json={"filename": "x.tar.gz"})
        assert r.status_code == 409


def test_second_backup_409s_while_first_holds_the_slot(monkeypatch):
    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(hb.threading, "Thread", _T)
    with _client() as client:
        r1 = client.post("/api/backup/now", json={"strategy": "full"})
        assert r1.status_code == 200
        r2 = client.post("/api/backup/now", json={"strategy": "full"})
        assert r2.status_code == 409


def test_reboot_failure_publishes_error(monkeypatch):
    class _T:
        def __init__(self, target=None, daemon=None, args=()):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(hb.threading, "Thread", _T)
    monkeypatch.setattr(hb.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        hb.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1})())
    with _client() as client:
        r = client.post("/api/system/reboot")
        assert r.status_code == 200
        assert hb.read_status()["status"] == "error"
        assert not hb.task_running()


if __name__ == "__main__":
    import traceback

    failed = 0
    # monkeypatch-using tests need pytest; the rest run here.
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if "monkeypatch" in fn.__code__.co_varnames:
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:
            print(f"  FAIL  {name}")
            traceback.print_exc()
            failed += 1
    sys.exit(1 if failed else 0)
