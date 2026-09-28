#!/usr/bin/env python3
"""Resume accepted media jobs and deliver receipts without an LLM turn."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import media_jobs as jobs
import picture

MAX_SEND_BYTES = 49 * 1024 * 1024
_idle_job = None
_idle_since = None


def unit_state(kind):
    result = subprocess.run(["systemctl", "is-active", f"homebrain-{kind}.service"],
                            capture_output=True, text=True, timeout=10)
    state = result.stdout.strip()
    if state not in ("active", "activating", "deactivating", "inactive", "failed"):
        raise RuntimeError("Cannot determine generation service state")
    return state


def chat_healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def chat_idle():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/slots", timeout=3) as r:
            slots = json.load(r)
        return (isinstance(slots, list) and bool(slots)
                and all(isinstance(s, dict) and s.get("is_processing") is False for s in slots))
    except (OSError, ValueError):
        return False


def handoff_ready(job):
    """Let the post-tool agent reply finish before stopping its model.

    Require ten continuous idle seconds, including after worker restarts.
    An unavailable slots endpoint fails closed until the acceptance deadline.
    """
    global _idle_job, _idle_since
    if _idle_job != job["id"]:
        _idle_job, _idle_since = job["id"], None
    if not chat_idle():
        _idle_since = None
        return False
    now = time.monotonic()
    if _idle_since is None:
        _idle_since = now
    return now - _idle_since >= 10


def artifact(job):
    path = picture.image_file(job["id"], kind=job["kind"])
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            header = f.read(24)
        if job["kind"] == "picture":
            valid = header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR"
        else:
            valid = header[4:8] == b"ftyp"
        meta = os.path.splitext(path)[0] + ".json"
        if not picture._inside(picture.pictures_dir(job["kind"]), meta):
            return None
        with open(meta) as f:
            saved = json.load(f)
        return path if valid and saved.get("id") == job["id"] else None
    except (OSError, ValueError):
        return None


def send(job, phase):
    """Never log CLI output: errors can include credentials or private paths."""
    if not job["target"] or jobs.delivery_target() != job["target"]:
        return "blocked", None, "Telegram destination is no longer authorized."
    name = picture.MODELS[job["model"]]["name"]
    if phase == "start":
        text = f"Starting your {job['kind']} with {name}. Chat will pause; the result will be sent here. Job {job['id']}."
        media = None
    else:
        media = artifact(job) if job["state"] == "succeeded" else None
        text = f"{name}: {job['message']} Job {job['id']}."
        if job["chat_ready"] == 0:
            text += " Chat has not recovered; check the dashboard."
        if job["state"] == "succeeded" and not media:
            return "failed", None, "Saved media is missing or invalid."
        if media and os.path.getsize(media) > MAX_SEND_BYTES:
            return "failed", None, "Media exceeds the upload limit; download it from the dashboard."
    cmd = ["sudo", "-H", "-u", "homebrain", "openclaw", "message", "send",
           "--channel", "telegram", "--account", "default", "--target", job["target"],
           "--message", text, "--json"]
    staging = None
    try:
        if media:
            # OpenClaw permits attachments below its media root, not the
            # dashboard gallery. Only stage this job's validated artifact.
            root = os.path.join(picture.home(), ".openclaw", "media")
            os.makedirs(root, mode=0o700, exist_ok=True)
            picture._give(root)
            staging = tempfile.mkdtemp(prefix="homebrain-", dir=root)
            picture._give(staging)
            attachment = os.path.join(staging, job["id"] + picture.extension(job["kind"]))
            shutil.copyfile(media, attachment)
            os.chmod(attachment, 0o600)
            picture._give(attachment)
            cmd += ["--media", attachment]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return "unknown", None, "Send timed out; delivery is unconfirmed."
    except OSError:
        return "pending", None, "Messaging executable unavailable."
    finally:
        if staging:
            shutil.rmtree(staging)
    try:
        # Some releases prefix JSON with plugin startup diagnostics.
        raw = result.stdout
        body = json.loads(raw[raw.index("{"):])
        receipt = body.get("payload", body.get("result", body))
        message_id = receipt.get("messageId") or receipt.get("message_id")
        if message_id:
            return "sent", str(message_id), ""
    except (ValueError, TypeError, AttributeError):
        pass
    # Retry only rejections known not to have delivered. Network/5xx errors
    # and unrecognized output can follow a successful send.
    error = (result.stderr + result.stdout).lower()
    if any(marker in error for marker in ("429", "too many requests", "econnrefused", "enotfound")):
        return "pending", None, "Telegram temporarily unavailable."
    if any(marker in error for marker in ("403", "401", "chat not found", "bot was blocked", "media path")):
        return "failed", None, "Telegram rejected delivery; check channel configuration."
    return "unknown", None, "Messaging returned no delivery receipt."


def deliver():
    for entry in jobs.pending_deliveries():
        job = jobs.get(entry["job_id"])
        if not jobs.claim_delivery(job["id"], entry["phase"]):
            continue
        state, message_id, error = send(job, entry["phase"])
        if state == "pending" and entry["attempts"] >= 7:
            state, error = "failed", "Delivery retries exhausted; result remains on the dashboard."
        jobs.delivery_result(job["id"], entry["phase"], state, message_id, error,
                             delay=min(3600, 15 * 2 ** entry["attempts"]))


def tick():
    job = jobs.active()
    if job:
        kind = job["kind"]
        if job["state"] == "accepted":
            if time.time() - job["created"] > 600:
                jobs.finish(job["id"], "failed", "Generation did not start before its deadline.", None)
            else:
                ready = not job["target"] or jobs.get(job["id"])["delivery"]["state"] == "sent"
                if ready and handoff_ready(job):
                    directory = picture.pictures_dir(kind)
                    os.makedirs(directory, mode=0o750, exist_ok=True)
                    if shutil.disk_usage(directory).free < 2 * 1024**3:
                        jobs.finish(job["id"], "failed", "Not enough disk space to generate media.", None)
                    elif not picture.weights_ready(kind=kind, model=job["model"]):
                        jobs.finish(job["id"], "failed", "The required model weights are missing.", None)
                    else:
                        # Persist before touching systemd. A crash in this window is
                        # reconciled as interrupted, never submitted a second time.
                        jobs.transition(job["id"], "starting")
                        picture.write_request({k: job[k] for k in ("id", "prompt", "seed", "model")}, kind=kind)
                        picture.write_status("running", "Preparing generation. Chat will pause.", job["id"], kind=kind)
                        subprocess.run(["systemctl", "reset-failed", f"homebrain-{kind}.service"],
                                       capture_output=True, timeout=10)
                        result = subprocess.run(["systemctl", "start", "--no-block", f"homebrain-{kind}.service"],
                                                capture_output=True, timeout=10)
                        if result.returncode:
                            jobs.finish(job["id"], "failed", "Generation service could not start.", None)
                        else:
                            jobs.transition(job["id"], "running")
        elif unit_state(kind) in ("inactive", "failed"):
            status = picture.read_status(kind=kind)
            healthy = chat_healthy()
            if not healthy:
                subprocess.run(["systemctl", "start", "--no-block", "llama-server.service"],
                               capture_output=True, timeout=10)
                # Release the slot only after a bounded recovery attempt. The
                # service also has ExecStopPost recovery for forced termination.
                if time.time() - job["updated"] < 120:
                    return
            saved = artifact(job)
            message = f"{kind.capitalize()} ready." if saved else f"The {kind} failed or was interrupted."
            if not saved and status.get("id") == job["id"] and status.get("state") == "error":
                # Backend errors stay in local logs/status, never become prompts.
                message = f"The {kind} failed. See the dashboard for details."
            jobs.finish(job["id"], "succeeded" if saved else "failed", message, healthy)
    deliver()


def main():
    os.makedirs(jobs.state_dir(), mode=0o700, exist_ok=True)
    with open(os.path.join(jobs.state_dir(), "worker.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs.recover_sends()
        while True:
            try:
                tick()
            except Exception:
                logging.exception("Media worker iteration failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
