"""Private durable media jobs and delivery receipts. No model or network calls."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import sqlite3
import time

import picture

ACTIVE = ("accepted", "starting", "running")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


class Busy(Exception):
    pass


def state_dir():
    return os.environ.get("HB_MEDIA_STATE_DIR", "/var/lib/homebrain/media")


@contextmanager
def connect():
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    path = os.path.join(state_dir(), "jobs.sqlite3")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, model TEXT NOT NULL,
                prompt TEXT NOT NULL, seed INTEGER NOT NULL,
                source TEXT NOT NULL, request_id TEXT NOT NULL, target TEXT NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL, chat_ready INTEGER,
                UNIQUE(source, request_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs ((1))
                WHERE state IN ('accepted', 'starting', 'running');
            CREATE TABLE IF NOT EXISTS deliveries (
                job_id TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                message_id TEXT, error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(job_id, phase)
            );
        """)
        with db:
            yield db
    finally:
        db.close()


def active():
    if not os.path.isfile(os.path.join(state_dir(), "jobs.sqlite3")):
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE state IN (?,?,?)", ACTIVE).fetchone()
        return dict(row) if row else None


def get(job_id):
    if not os.path.isfile(os.path.join(state_dir(), "jobs.sqlite3")):
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        row = db.execute("SELECT * FROM deliveries WHERE job_id=? ORDER BY phase LIMIT 1",
                         (job_id,)).fetchone()
        result["delivery"] = dict(row) if row else None
        return result


def public(job):
    result = {k: job[k] for k in ("id", "kind", "model", "state", "message", "chat_ready")}
    delivery = job.get("delivery")
    result["delivery"] = ({k: delivery[k] for k in ("phase", "state", "error")}
                          if delivery else None)
    result["automatic_delivery"] = bool(job["target"])
    return result


def accept(payload, busy=lambda: False):
    """Call under the manager's shared task lock. Replays precede busy checks."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM jobs WHERE source=? AND request_id=?",
                         (payload["source"], payload["request_id"])).fetchone()
        if old:
            if any(old[k] != payload[k] for k in ("kind", "model", "prompt", "target")):
                raise ValueError("request_id already belongs to a different request")
            return dict(old)
        if db.execute("SELECT 1 FROM jobs WHERE state IN (?,?,?)", ACTIVE).fetchone() or busy():
            raise Busy("A task is already running")
        now = time.time()
        db.execute("""INSERT INTO jobs
            (id,kind,model,prompt,seed,source,request_id,target,state,created,updated)
            VALUES (?,?,?,?,?,?,?,?,'accepted',?,?)""",
            tuple(payload[k] for k in ("id", "kind", "model", "prompt", "seed",
                                      "source", "request_id", "target")) + (now, now))
        if payload["target"]:
            db.execute("INSERT INTO deliveries(job_id,phase,state) VALUES (?,'start','pending')",
                       (payload["id"],))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (payload["id"],)).fetchone())


def transition(job_id, state):
    with connect() as db:
        db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (state, time.time(), job_id))


def finish(job_id, state, message, chat_ready):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE jobs SET state=?,message=?,chat_ready=?,updated=? WHERE id=?",
                   (state, message, chat_ready, time.time(), job_id))
        db.execute("""INSERT OR IGNORE INTO deliveries(job_id,phase,state)
            SELECT id,'done','pending' FROM jobs WHERE id=? AND target != ''""", (job_id,))
        db.execute("UPDATE deliveries SET state='cancelled' WHERE job_id=? AND phase='start' AND state='pending'",
                   (job_id,))


def pending_deliveries():
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT * FROM deliveries
            WHERE state='pending' AND next_attempt<=? ORDER BY next_attempt""", (time.time(),))]


def delivery_result(job_id, phase, state, message_id=None, error="", delay=0):
    with connect() as db:
        db.execute("""UPDATE deliveries SET state=?,message_id=?,error=?,next_attempt=?
            WHERE job_id=? AND phase=?""",
            (state, message_id, error, time.time() + delay, job_id, phase))


def claim_delivery(job_id, phase):
    with connect() as db:
        return db.execute("""UPDATE deliveries SET state='sending',attempts=attempts+1
            WHERE job_id=? AND phase=? AND state='pending'""", (job_id, phase)).rowcount == 1


def recover_sends():
    with connect() as db:
        db.execute("""UPDATE deliveries SET state='unknown',error='Interrupted send; delivery is unconfirmed.'
            WHERE state='sending'""")


def retry_delivery(job_id):
    job = get(job_id)
    if not job or not job.get("delivery"):
        return False
    phase = job["delivery"]["phase"]
    with connect() as db:
        return db.execute("""UPDATE deliveries SET state='pending',attempts=0,next_attempt=0,error=''
            WHERE job_id=? AND phase=? AND state IN ('failed','unknown','blocked')""",
            (job_id, phase)).rowcount == 1


def recent(limit=10):
    if not os.path.isfile(os.path.join(state_dir(), "jobs.sqlite3")):
        return []
    with connect() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]
    return [get(job_id) for job_id in ids]


def telegram_targets():
    """Only numeric paired DMs on the default account; never group/usernames."""
    root = os.path.join(picture.home(), ".openclaw")
    try:
        with open(os.path.join(root, "openclaw.json")) as f:
            tg = json.load(f).get("channels", {}).get("telegram", {})
        if not tg or tg.get("enabled") is False:
            return []
        account = (tg.get("accounts") or {}).get("default", tg)
        if account.get("enabled") is False:
            return []
        allow = list(account.get("allowFrom") or [])
        try:
            with open(os.path.join(root, "credentials", "telegram-default-allowFrom.json")) as f:
                allow += json.load(f).get("allowFrom", [])
        except (OSError, ValueError):
            pass
        return sorted({str(v) for v in allow if re.fullmatch(r"[1-9][0-9]{0,19}", str(v))})
    except (OSError, ValueError, TypeError):
        return []


def delivery_target():
    try:
        with open(os.path.join(state_dir(), "delivery.json")) as f:
            target = json.load(f)["target"]
        return target if target in telegram_targets() else ""
    except (OSError, ValueError, KeyError):
        return ""


def set_delivery_target(target):
    if not isinstance(target, str) or (target and target not in telegram_targets()):
        raise ValueError("Choose a paired Telegram DM.")
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    path = os.path.join(state_dir(), "delivery.json")
    fd = os.open(path + ".tmp", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"target": target}, f)
    os.replace(path + ".tmp", path)
