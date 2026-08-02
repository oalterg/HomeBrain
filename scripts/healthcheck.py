#!/usr/bin/env python3
"""HomeBrain health checker + notifier.

Runs as root from homebrain-health.timer (every 30 min). Evaluates a small,
fixed set of trust-critical checks — backups, disks, SMART, services,
containers, updates — writes a machine-readable summary for the dashboard
banner, and pushes plain-language alerts to the owner through the OpenClaw
Telegram channel when something *changes*.

Design constraints:
  - stdlib only; no venv, no pip deps (runs via /usr/bin/python3).
  - Deterministic delivery: `openclaw message send` goes straight through the
    gateway to the channel — no LLM in the loop, so alerts still arrive when
    llama-server is down.
  - Notify on level transitions, not on every run. Steady-state reminders:
    critical every 24 h, warnings every 7 days. State in
    /var/lib/homebrain/health_state.json.
  - Every check degrades gracefully: a missing tool (smartctl, docker,
    openclaw) skips its check instead of erroring the whole run. The
    dashboard banner (/api/health reading health.json) is the universal
    fallback when no messenger channel is linked.
"""

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

INSTALL_DIR = "/opt/homebrain"
ENV_FILE = f"{INSTALL_DIR}/.env"
VERSION_FILE = f"{INSTALL_DIR}/version.json"
STATE_DIR = "/var/lib/homebrain"
HEALTH_FILE = f"{STATE_DIR}/health.json"
STATE_FILE = f"{STATE_DIR}/health_state.json"
BACKUP_DIR = "/mnt/backup"
# Where the files live unless the owner moved them to a drive of their own.
NC_DATA_DEFAULT = "/home/homebrain/nextcloud-data"
BACKUP_LOG = "/var/log/homebrain/backup.log"
BACKUP_CRON_FILE = "/etc/cron.d/homebrain-backup"
OFFSITE_STATE = f"{STATE_DIR}/offsite.json"
# PID of a mirror that is running right now, published by
# common.sh:offsite_mirror. Deliberately NOT the mirror's lock file: probing
# that with a real flock means briefly holding it, and a mirror starting in
# that window would find it taken, log "already running" and return success —
# so running a health check would silently skip an off-site copy.
OFFSITE_RUN_FILE = "/var/run/homebrain-offsite.running"
REBOOT_REQUIRED_FILE = "/var/run/reboot-required"
HOMEBRAIN_HOME = "/home/homebrain"
OPENCLAW_DIR = f"{HOMEBRAIN_HOME}/.openclaw"
OPENCLAW_PORT = 18789
REPO_API_URL = "https://api.github.com/repos/oalterg/HomeBrain"

DAY = 86400
REMIND_SECS = {"crit": 1 * DAY, "warn": 7 * DAY, "info": 7 * DAY}
UPDATE_CHECK_SECS = 1 * DAY

LEVEL_RANK = {"ok": 0, "info": 1, "warn": 2, "crit": 3}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in scripts/tests/test_healthcheck.py)
# ---------------------------------------------------------------------------

def parse_env(text):
    """Parse .env KEY=VALUE lines, stripping single/double quotes."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def expected_backup_interval(env):
    """Seconds between scheduled backups, derived from the cron fields the
    dashboard writes to .env. Daily unless a day-of-week or day-of-month
    restriction makes it weekly/monthly."""
    if env.get("BACKUP_DAY_MONTH", "*") != "*":
        return 31 * DAY
    if env.get("BACKUP_DAY_WEEK", "*") != "*":
        return 7 * DAY
    return 1 * DAY


def backup_log_outcome(tail):
    """Classify the last backup run from the log tail: 'complete' if the
    newest run finished, 'started' if a run began without finishing (failed
    or still running — caller disambiguates via log mtime), 'none' if no
    run markers found."""
    last_start = tail.rfind("=== Starting Backup")
    last_done = tail.rfind("=== Backup Complete")
    if last_start == -1 and last_done == -1:
        return "none"
    if last_done > last_start:
        return "complete"
    return "started"


def disk_level(percent):
    if percent >= 95:
        return "crit"
    if percent >= 85:
        return "warn"
    return "ok"


def decide_notification(prev, level, now):
    """Decide whether a check's current level warrants a push.

    prev: {'level': str, 'last_notified': epoch} or None (first sighting).
    Returns 'alert' (escalation, or steady-state reminder past its window),
    'recovery' (crit went back to ok), or None.
    """
    prev_level = prev.get("level", "ok") if prev else "ok"
    prev_rank, rank = LEVEL_RANK.get(prev_level, 0), LEVEL_RANK.get(level, 0)
    if rank > prev_rank:
        return "alert"
    if rank == 0:
        return "recovery" if prev_level == "crit" else None
    last = prev.get("last_notified", 0) if prev else 0
    if now - last >= REMIND_SECS[level]:
        return "alert"
    return None


# ---------------------------------------------------------------------------
# Checks — each returns {"id", "level", "summary"} (or None to skip)
# ---------------------------------------------------------------------------

def check_disk(path, label, check_id):
    try:
        total, used, _ = shutil.disk_usage(path)
    except OSError:
        return None
    percent = round(used / total * 100)
    return {"id": check_id, "level": disk_level(percent),
            "summary": f"{label} is {percent}% full"}


def check_files_drive(env):
    """The files drive, when there is one, is either mounted or the box is
    down. Nextcloud refuses to start without the .ncdata marker its data
    directory carries, so an absent drive is a hard stop — and until this
    check existed it was a silent one: every container reports "running",
    the container health check has not failed yet, and nothing on the
    dashboard says the drive is gone."""
    path = env.get("NEXTCLOUD_DATA_DIR", "")
    if not path or path == NC_DATA_DEFAULT:
        return None          # files are on the internal disk; nothing to lose
    if os.path.ismount(path):
        return check_disk(path, "Files drive", "disk_files")
    return {"id": "disk_files", "level": "crit",
            "summary": f"Files drive not connected ({path}) — Nextcloud cannot start"}


def check_backup(env, now):
    scheduled = os.path.exists(BACKUP_CRON_FILE) or \
        os.path.exists("/etc/systemd/system/homebrain-backup.timer")
    if not scheduled:
        return {"id": "backup", "level": "warn",
                "summary": "Automatic backups are not set up"}
    # Internal-storage mode (no drive): the directory lives on the root disk,
    # so "not mounted" is normal and disk_root covers the space check.
    internal = env.get("BACKUP_INTERNAL", "false").lower() == "true"
    if not internal and not os.path.ismount(BACKUP_DIR):
        return {"id": "backup", "level": "crit",
                "summary": "Backup drive is not connected"}

    # Pre-update system snapshots don't contain the user's files — they must
    # not satisfy "your data is backed up".
    archives = [a for a in
                glob.glob(f"{BACKUP_DIR}/homebrain_backup*.tar.gz*") +
                glob.glob(f"{BACKUP_DIR}/nextcloud_backup*.tar.gz*")
                if "_system_" not in os.path.basename(a)]
    newest = max((os.path.getmtime(a) for a in archives), default=0)

    # A run that started after the last success and isn't recent is a failure.
    try:
        with open(BACKUP_LOG, "rb") as f:
            f.seek(max(0, os.path.getsize(BACKUP_LOG) - 65536))
            tail = f.read().decode(errors="replace")
        outcome = backup_log_outcome(tail)
        log_age = now - os.path.getmtime(BACKUP_LOG)
        if outcome == "started" and log_age > 6 * 3600:
            return {"id": "backup", "level": "crit",
                    "summary": "The last backup failed — check the backup log"}
    except OSError:
        pass

    if not newest:
        return {"id": "backup", "level": "warn",
                "summary": "No backup has been made yet"}
    age = now - newest
    interval = expected_backup_interval(env)
    days = int(age // DAY)
    if age > 3 * interval:
        return {"id": "backup", "level": "crit",
                "summary": f"No backup for {days} days — backups appear to have stopped"}
    if age > interval + 12 * 3600:
        return {"id": "backup", "level": "warn",
                "summary": f"Last backup is {days} days old (schedule looks overdue)"}
    return {"id": "backup", "level": "ok", "summary": "Backups are up to date"}


def offsite_is_syncing():
    """True if a mirror is running right now.

    Same predicate as app.py:offsite_is_syncing() — a read of the PID file,
    never a probe of the lock (see OFFSITE_RUN_FILE)."""
    try:
        with open(OFFSITE_RUN_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    return os.path.isdir(f"/proc/{pid}")


def check_offsite(env, now):
    """Warn-only: the local backup is the data protection; off-site is a copy.

    A mirror that is *currently running* outranks whatever the state file says,
    because that file describes the previous run. Without this, the hours-long
    upload of a multi-GB archive reports "the last off-site copy failed" for
    its entire duration — the check would be warning about the very thing it
    is watching succeed, and a warning that is wrong on a schedule is how real
    ones get ignored."""
    if env.get("OFFSITE_ENABLED", "false").lower() != "true":
        return None
    syncing = offsite_is_syncing()
    try:
        with open(OFFSITE_STATE) as f:
            st = json.load(f)
    except Exception:
        if syncing:
            return {"id": "offsite", "level": "ok",
                    "summary": "First off-site copy is in progress"}
        return {"id": "offsite", "level": "warn",
                "summary": "Off-site copy is enabled but has not run yet"}
    if not st.get("ok"):
        if syncing:
            return {"id": "offsite", "level": "ok",
                    "summary": "Off-site copy in progress (retrying after a failure)"}
        return {"id": "offsite", "level": "warn",
                "summary": "The last off-site copy failed — local backups are unaffected"}
    age = now - st.get("ts", 0)
    if age > expected_backup_interval(env) + DAY:
        if syncing:
            return {"id": "offsite", "level": "ok",
                    "summary": "Off-site copy in progress (catching up)"}
        days = int(age // DAY)
        return {"id": "offsite", "level": "warn",
                "summary": f"No off-site copy for {days} days"}
    return {"id": "offsite", "level": "ok", "summary": "Off-site copy is up to date"}


def check_smart():
    """SMART health of physical disks. Skips silently when smartmontools is
    missing or a device doesn't speak SMART (USB bridges commonly don't)."""
    if not shutil.which("smartctl"):
        return None
    failed = []
    for dev in sorted(glob.glob("/sys/block/sd*") + glob.glob("/sys/block/nvme*n*")):
        name = os.path.basename(dev)
        if re.match(r"nvme\d+n\d+p", name):
            continue
        try:
            out = subprocess.run(
                ["smartctl", "-H", "-j", f"/dev/{name}"],
                capture_output=True, text=True, timeout=30)
            data = json.loads(out.stdout or "{}")
            status = data.get("smart_status")
            if status is not None and status.get("passed") is False:
                failed.append(name)
        except Exception:
            continue
    if failed:
        return {"id": "smart", "level": "crit",
                "summary": f"Drive {', '.join(failed)} is reporting SMART failure — replace it and check your backups"}
    return {"id": "smart", "level": "ok", "summary": "Drives report healthy"}


def _unit_exists_and_enabled(unit):
    try:
        out = subprocess.run(["systemctl", "is-enabled", unit],
                             capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return out.returncode == 0 and out.stdout.strip() not in ("masked", "disabled")


def check_services(has_gpu):
    units = ["docker", "homebrain-manager"]
    if has_gpu:
        units += ["llama-server", "whisper-server"]
    down = []
    for unit in units:
        if not _unit_exists_and_enabled(unit):
            continue
        try:
            active = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                                    timeout=10).returncode == 0
        except Exception:
            continue
        if not active:
            down.append(unit)
    if down:
        return {"id": "services", "level": "crit",
                "summary": f"Service not running: {', '.join(down)}"}
    return {"id": "services", "level": "ok", "summary": "System services running"}


def check_openclaw_gateway(has_gpu):
    if not (has_gpu and shutil.which("openclaw")):
        return None
    try:
        with socket.create_connection(("127.0.0.1", OPENCLAW_PORT), timeout=3):
            pass
        return {"id": "openclaw", "level": "ok", "summary": "AI assistant reachable"}
    except OSError:
        return {"id": "openclaw", "level": "warn",
                "summary": "AI assistant gateway is not responding"}


def check_containers():
    if not shutil.which("docker"):
        return None
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
    except Exception:
        return None
    restarting, unhealthy = [], []
    for line in out.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 3 or not parts[0].startswith("homebrain"):
            continue
        name = parts[0].split("-")[1] if "-" in parts[0] else parts[0]
        if parts[1] == "restarting":
            restarting.append(name)
        elif "(unhealthy)" in parts[2]:
            unhealthy.append(name)
    if restarting:
        return {"id": "containers", "level": "crit",
                "summary": f"Service crash-looping: {', '.join(restarting)}"}
    if unhealthy:
        return {"id": "containers", "level": "warn",
                "summary": f"Service unhealthy: {', '.join(unhealthy)}"}
    return {"id": "containers", "level": "ok", "summary": "Containers healthy"}


def release_key(tag):
    """Sortable key for a release tag (v2026.07.21, v1.1.0, v0.1).

    Returns None for anything that isn't a dotted numeric tag, which the
    caller treats as "cannot order" rather than "differs".
    """
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (tag or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def check_update(state, now):
    """Once a day, on the stable channel only: is a newer release out?
    info-level — surfaces in the banner and one push per new version."""
    try:
        with open(VERSION_FILE) as f:
            version = json.load(f)
    except Exception:
        return None
    if version.get("channel") != "stable":
        return None
    installed = version.get("ref", "")

    # The cached tag is only meaningful for the version it was compared
    # against. Without this an update installed since the last probe leaves a
    # day-old `latest` in place that can predate what is now on disk.
    upd = state.setdefault("update", {})
    fresh = now - upd.get("ts", 0) < UPDATE_CHECK_SECS
    if fresh and upd.get("latest") and upd.get("installed") == installed:
        latest = upd["latest"]
    else:
        try:
            req = urllib.request.Request(f"{REPO_API_URL}/releases/latest",
                                         headers={"User-Agent": "homebrain-health"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                latest = json.load(resp).get("tag_name", "")
            upd["ts"], upd["latest"], upd["installed"] = now, latest, installed
        except Exception:
            return None

    # Order the tags rather than comparing them for inequality: a plain !=
    # reports *any* difference as an available update, including a release
    # older than the one installed. Updates are one-way (see the downgrade
    # guard in common.sh), so nagging the user toward an older tag sends them
    # at something update.sh will refuse. Unorderable tags stay quiet.
    cur, new = release_key(installed), release_key(latest)
    if cur and new and new > cur:
        return {"id": "update", "level": "info",
                "summary": f"Update {latest} is available — install it from the dashboard"}
    return {"id": "update", "level": "ok", "summary": "HomeBrain is up to date"}


def check_reboot():
    """unattended-upgrades never reboots on its own (deliberate — this is a
    NAS), so a kernel/libc patch waits invisibly until the next manual
    restart. Surface it. warn-only: the box keeps working, just on old code."""
    if not os.path.exists(REBOOT_REQUIRED_FILE):
        return {"id": "reboot", "level": "ok", "summary": "No restart pending"}
    pkgs = []
    try:
        with open(REBOOT_REQUIRED_FILE + ".pkgs") as f:
            pkgs = sorted({line.strip() for line in f if line.strip()})
    except OSError:
        pass
    detail = f" ({', '.join(pkgs[:3])})" if pkgs else ""
    return {"id": "reboot", "level": "warn",
            "summary": f"Restart this box to finish OS security updates{detail}"}


# ---------------------------------------------------------------------------
# Push delivery via OpenClaw
# ---------------------------------------------------------------------------

def resolve_push_target():
    """(channel, target) for the owner. Telegram only: pairing lands in
    credentials/telegram-default-allowFrom.json; a hand-set allowFrom in
    openclaw.json is the fallback."""
    try:
        with open(f"{OPENCLAW_DIR}/openclaw.json") as f:
            channels = json.load(f).get("channels", {})
    except Exception:
        return None
    tg = channels.get("telegram", {})
    if tg.get("enabled"):
        for source in (f"{OPENCLAW_DIR}/credentials/telegram-default-allowFrom.json",):
            try:
                with open(source) as f:
                    allow = json.load(f).get("allowFrom", [])
                if allow:
                    return ("telegram", str(allow[0]))
            except Exception:
                pass
        if tg.get("allowFrom"):
            return ("telegram", str(tg["allowFrom"][0]))
    return None


def send_push(channel, target, text):
    """Deterministic send through the gateway (no agent turn involved)."""
    cmd = ["sudo", "-H", "-u", "homebrain", "timeout", "45",
           "openclaw", "message", "send", "--channel", channel,
           "--target", target, "-m", text, "--json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        log(f"[WARN] push failed to spawn: {e}")
        return False
    if out.returncode == 0 and '"messageId"' in out.stdout:
        return True
    log(f"[WARN] push failed (rc={out.returncode}): {out.stdout[-300:]} {out.stderr[-300:]}")
    return False


# ---------------------------------------------------------------------------
# Email delivery — the second transport
# ---------------------------------------------------------------------------
# A notification system with one channel that the owner can silently uninstall
# is not one. Push goes out through the agent's Telegram link, which lives on
# this box: unlink it, or break OpenClaw, and the box loses its voice at
# exactly the moment it has something to say. SMTP credentials are already in
# the stack, so the second transport costs a function rather than a service.
#
# Fallback, not fan-out. Email fires only when the push did not go — the owner
# who wired Telegram wants Telegram, and two copies of every warning is how a
# notification channel gets muted.

EMAIL_ACCOUNTS_FILE = f"{OPENCLAW_DIR}/email_accounts.json"


def resolve_email_target(env):
    """(account, recipient) for the owner, or None if email cannot be sent.

    Recipient is NOTIFY_EMAIL if set, else the address used at registration.
    """
    to = (env.get("NOTIFY_EMAIL") or env.get("CLOUD_EMAIL") or "").strip()
    if not to:
        return None
    try:
        with open(EMAIL_ACCOUNTS_FILE) as f:
            accounts = json.load(f).get("accounts", [])
    except Exception:
        return None
    for a in accounts:
        if a.get("smtp_host") and a.get("user"):
            return (a, to)
    return None


def send_email(env, account, to, subject, text):
    """Send one plain-text message over SMTP. True if the server accepted it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import smtplib
        from email.message import EmailMessage

        from mcp_common import decrypt_secret
    except Exception as e:
        log(f"[WARN] email unavailable: {e}")
        return False

    password = decrypt_secret(
        account.get("smtp_password", "") or account.get("imap_password", ""),
        env.get("HOMEBRAIN_EMAIL_KEY", ""))
    if not password:
        # decrypt_secret returns "" rather than ciphertext when the key is
        # wrong — sending that as a password would fail at the SMTP server
        # with nothing pointing at the real cause.
        log("[WARN] email skipped: the stored SMTP password could not be decrypted")
        return False

    msg = EmailMessage()
    msg["From"] = account["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)

    host = account["smtp_host"]
    port = int(account.get("smtp_port", 587))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(account["user"], password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(account["user"], password)
                s.send_message(msg)
    except Exception as e:
        log(f"[WARN] email send failed: {e}")
        return False
    return True


def email_subject(alerts, recoveries):
    if any(a["level"] == "crit" for a in alerts):
        return "HomeBrain needs attention"
    if alerts:
        return "HomeBrain: something to look at"
    return "HomeBrain: resolved"


# ---------------------------------------------------------------------------
# Dead-man's switch
# ---------------------------------------------------------------------------
# Every alert in this file is sent BY the box. A box that is off, unplugged,
# stolen, or whose disk has died sends nothing — and silence is indistinguish-
# able from healthy. That is the one failure the owner finds out about weeks
# later, usually when they need a backup that stopped being made in March.
#
# The fix has to live somewhere that is not this box. The registrar Worker is
# already deployed and already knows the owner's email, so the device half is
# one POST on the timer that is already running: no new service, no new
# credential, no new schedule. If the Worker stops hearing from us, IT sends
# the mail.
#
# Off unless HEARTBEAT_URL is set, because the Worker's half is deployed by the
# owner into their own Cloudflare account. See registrar/worker.js.

FACTORY_CONFIG = ("/boot/firmware/factory_config.txt"
                  if os.path.isdir("/boot/firmware")
                  else f"{INSTALL_DIR}/factory_config.txt")


def factory_config():
    try:
        with open(FACTORY_CONFIG) as f:
            return parse_env(f.read())
    except OSError:
        return {}


def heartbeat_url(env, factory):
    """Where to POST, or "" when the switch is not armed."""
    explicit = env.get("HEARTBEAT_URL", "").strip()
    if explicit:
        return explicit
    if env.get("HEARTBEAT_ENABLED", "false").lower() != "true":
        return ""
    base = factory.get("REGISTRAR_URL", "").strip().rstrip("/")
    return f"{base}/heartbeat" if base else ""


def send_heartbeat(env, factory, overall, now):
    """One POST saying "still here". Never fatal: a heartbeat that cannot be
    delivered must not stop the box checking its own health."""
    url = heartbeat_url(env, factory)
    if not url:
        return None
    secret = factory.get("REGISTRAR_SECRET", "")
    if not secret:
        log("[WARN] heartbeat configured but no REGISTRAR_SECRET — not sending")
        return False
    payload = json.dumps({
        "device_id": factory.get("NEWT_ID", "unknown"),
        "overall": overall,
        "ts": int(now),
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log(f"[WARN] heartbeat failed: {e}")
        return False


def compose_message(alerts, recoveries):
    lines = []
    if alerts:
        levels = {a["level"] for a in alerts}
        serious = levels & {"warn", "crit"}
        icon = "🚨" if "crit" in levels else ("⚠️" if "warn" in levels else "ℹ️")
        lines.append(f"{icon} HomeBrain needs attention:" if serious
                     else f"{icon} HomeBrain:")
        lines += [f"• {a['summary']}" for a in alerts]
        if serious:
            lines.append("Open your dashboard for details.")
    if recoveries:
        if lines:
            lines.append("")
        lines.append("✅ Resolved: " + "; ".join(r["summary"] for r in recoveries))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def atomic_write_json(path, data, mode=0o644):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def has_gpu(env):
    if env.get("HAS_GPU", "").lower() == "true":
        return True
    # The platform record written by detect_platform() in common.sh. Globbing
    # /dev/dri/renderD* used to stand in for this, but a render node is not a
    # compute GPU — an RPi's VideoCore has one.
    try:
        with open(f"{INSTALL_DIR}/.platform.json") as f:
            return bool(json.load(f).get("has_gpu"))
    except (OSError, ValueError):
        return False


def main():
    global BACKUP_DIR
    now = time.time()
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(ENV_FILE) as f:
            env = parse_env(f.read())
    except OSError:
        env = {}
    BACKUP_DIR = env.get("BACKUP_MOUNTDIR", BACKUP_DIR)
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("checks", {})

    gpu = has_gpu(env)
    checks = [c for c in [
        check_backup(env, now),
        check_offsite(env, now),
        check_disk("/", "System disk", "disk_root"),
        check_disk(BACKUP_DIR, "Backup drive", "disk_backup") if os.path.ismount(BACKUP_DIR) else None,
        check_files_drive(env),
        check_smart(),
        check_services(gpu),
        check_containers(),
        check_openclaw_gateway(gpu),
        check_update(state, now),
        check_reboot(),
    ] if c]

    overall = "ok"
    for c in checks:
        if LEVEL_RANK.get(c["level"], 0) > LEVEL_RANK.get(overall, 0):
            overall = c["level"]

    # Decide pushes per check, then batch into one message.
    alerts, recoveries = [], []
    for c in checks:
        prev = state["checks"].get(c["id"])
        action = decide_notification(prev, c["level"], now)
        entry = {"level": c["level"],
                 "last_notified": prev.get("last_notified", 0) if prev else 0}
        if action == "alert":
            alerts.append(c)
            entry["last_notified"] = now
        elif action == "recovery":
            recoveries.append(c)
        state["checks"][c["id"]] = entry

    push = resolve_push_target()
    pushed = False
    push_enabled = env.get("NOTIFY_PUSH", "true").lower() != "false"
    if (alerts or recoveries) and push and push_enabled:
        text = compose_message(alerts, recoveries)
        pushed = send_push(push[0], push[1], text)
        log(f"[INFO] push via {push[0]}: {'sent' if pushed else 'FAILED'} — {len(alerts)} alert(s), {len(recoveries)} recovery(ies)")
    # Second transport. Only when the push did not go — see the note above
    # resolve_email_target. A box whose Telegram link is gone, or whose
    # OpenClaw is broken, still gets to say so.
    emailed = False
    email_enabled = env.get("NOTIFY_EMAIL_ENABLED", "true").lower() != "false"
    if (alerts or recoveries) and not pushed and email_enabled:
        mail = resolve_email_target(env)
        if mail:
            emailed = send_email(env, mail[0], mail[1],
                                 email_subject(alerts, recoveries),
                                 compose_message(alerts, recoveries))
            log(f"[INFO] email to {mail[1]}: {'sent' if emailed else 'FAILED'}")
        else:
            log("[INFO] no email fallback configured (needs a connected email "
                "account and NOTIFY_EMAIL or CLOUD_EMAIL)")

    if (alerts or recoveries) and not pushed and not emailed:
        log(f"[WARN] {len(alerts)} alert(s) reached NOBODY — dashboard banner only")

    # Unconditional: the heartbeat says "this box is still running", which is
    # exactly as true on a healthy run as on a failing one. Gating it on alerts
    # would mean a healthy box looks dead.
    beat = send_heartbeat(env, factory_config(), overall, now)
    if beat is not None:
        log(f"[INFO] heartbeat: {'sent' if beat else 'FAILED'}")

    atomic_write_json(HEALTH_FILE, {
        "ts": int(now),
        "overall": overall,
        "checks": [{"id": c["id"], "level": c["level"], "summary": c["summary"]}
                   for c in checks],
        "push_channel": push[0] if push and push_enabled else None,
        "last_delivery": "push" if pushed else ("email" if emailed else None),
    })
    atomic_write_json(STATE_FILE, state, mode=0o600)
    log(f"[INFO] health: {overall} — " +
        "; ".join(f"{c['id']}={c['level']}" for c in checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
