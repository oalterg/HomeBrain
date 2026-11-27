"""Daily graceful poweroff with an RTC wake alarm. Driven by systemd, never Flask.

The host must pass an observed wake test before a recurring schedule can run.
State is local to this machine and intentionally excluded from backups.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(os.environ.get("INSTALL_DIR", "/opt/homebrain"))
STATE_DIR = Path(os.environ.get("HOMEBRAIN_STATE_DIR", "/var/lib/homebrain"))
STATE = STATE_DIR / "nighttime.json"
RTC = Path("/sys/class/rtc/rtc0/wakealarm")
TIMER = "homebrain-nighttime.timer"
PACKAGE_LOCKS = ("/var/lib/dpkg/lock", "/var/lib/dpkg/lock-frontend", "/var/lib/apt/lists/lock")
DEFAULT = {"enabled": False, "start": "23:00", "end": "07:00", "timezone": "Europe/Berlin"}
# A busy backup, apt run or service restart at the start minute retries each
# tick for this long before the night is skipped.
RETRY = 900


class NighttimeError(Exception):
    pass


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        return default


def load():
    return json.loads(read(STATE, "{}"))


def save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=STATE_DIR, prefix=".nighttime-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, STATE)
        directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(name="nighttime", exclusive=True):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / (name + ".lock")).open("a") as f:
        try:
            fcntl.flock(f, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NighttimeError("A protected operation is running; nighttime will stay awake.") from None
        yield


@contextmanager
def operation():
    # Before installation there is no power controller to coordinate with.
    if not STATE.exists():
        yield
    else:
        with locked("power", exclusive=False):
            yield


def command(*args, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise NighttimeError(f"{args[0]} failed: {(result.stderr or result.stdout).strip()[:200]}")
    return result.stdout.strip()


def boot_id():
    return read("/proc/sys/kernel/random/boot_id")


def fingerprint():
    fields = [read("/etc/machine-id"), platform.release(),
              read("/sys/class/dmi/id/product_uuid"), read("/sys/class/dmi/id/bios_version")]
    return hashlib.sha256("|".join(fields).encode()).hexdigest()


def capability():
    available = platform.system() == "Linux" and platform.machine() == "x86_64" and RTC.exists()
    return {"available": available, "mode": "poweroff", "board": read("/sys/class/dmi/id/board_name"),
            "hint": "Set RTC Alarm Power On to By OS in firmware, then run and confirm a wake test."
            if available else "Scheduled poweroff requires an x86 Linux host with an RTC wake alarm."}


def validate(body):
    if not isinstance(body, dict) or set(body) - set(DEFAULT):
        raise NighttimeError("Use only enabled, start, end and timezone.")
    config = DEFAULT | body
    if type(config["enabled"]) is not bool:
        raise NighttimeError("enabled must be true or false.")
    for key in ("start", "end"):
        if not isinstance(config[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config[key]):
            raise NighttimeError("Times must use HH:MM in 24-hour format.")
    minutes = lambda t: int(t[:2]) * 60 + int(t[3:])
    duration = (minutes(config["end"]) - minutes(config["start"])) % 1440
    if not 30 <= duration <= 1380:
        raise NighttimeError("Choose a nighttime interval between 30 minutes and 23 hours.")
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise NighttimeError("Choose a valid IANA timezone, such as Europe/Berlin.") from None
    return config


def wall_time(day, hhmm, zone):
    # Round-trip through UTC detects a missing spring-forward minute. First
    # fold on autumn's repeated hour; advance a missing boundary to first valid.
    naive = datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())
    for _ in range(181):
        local = naive.replace(tzinfo=zone, fold=0)
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            return int(local.timestamp())
        naive += timedelta(minutes=1)
    raise NighttimeError("Could not resolve this local time.")


def next_cycle(config, after):
    zone = ZoneInfo(config["timezone"])
    day = datetime.fromtimestamp(after, zone).date()
    for offset in range(4):
        start_day = day + timedelta(days=offset)
        end_day = start_day + timedelta(days=config["end"] <= config["start"])
        start = wall_time(start_day, config["start"], zone)
        end = wall_time(end_day, config["end"], zone)
        if start > after and end - start >= 1800:
            return {"start": start, "wake": end}
    raise NighttimeError("Could not calculate the next nighttime interval.")


def trusted(state):
    return state.get("verified", {}).get("fingerprint") == fingerprint()


def status():
    state = load()
    config = DEFAULT | state.get("config", {})
    env = environment()
    backup_time = f"{int(env.get('BACKUP_HOUR', '3')):02}:{int(env.get('BACKUP_MINUTE', '0')):02}"
    return {"config": config, "capability": capability(), "verified": trusted(state),
            "backup_time": backup_time, "backup_timezone": datetime.now().astimezone().tzname(),
            "next": state.get("next") if config["enabled"] else None,
            "pending": state.get("pending"), "last_result": state.get("last_result"),
            "test_candidate": state.get("test_candidate"), "skip_start": state.get("skip_start")}


def configure(body):
    with locked():
        state = load()
        if state.get("pending"):
            raise NighttimeError("Cancel the pending wake test before changing the schedule.")
        config = validate((DEFAULT | state.get("config", {})) | body) if isinstance(body, dict) else validate(body)
        if config["enabled"]:
            if not capability()["available"] or not trusted(state):
                raise NighttimeError("Run and confirm a successful wake test on this hardware first.")
            command("systemctl", "enable", "--now", TIMER)
            command("systemctl", "is-active", TIMER)
        state.update(config=config, next=next_cycle(config, time.time() + 60), boot_id=boot_id())
        state.pop("skip_start", None)
        save(state)
    return status()


def skip():
    with locked():
        state = load()
        pending = state.get("pending")
        if pending:
            if pending.get("phase") != "countdown":
                raise NighttimeError("The power transition has already begun.")
            state.pop("pending")
            state["last_result"] = {"result": "cancelled", "message": "Wake test cancelled.", "at": time.time()}
        elif state.get("config", {}).get("enabled") and state.get("next"):
            state["skip_start"] = state["next"]["start"]
        else:
            raise NighttimeError("There is no scheduled nighttime interval to skip.")
        save(state)
    return status()


def test():
    with locked():
        state = load()
        if not capability()["available"]:
            raise NighttimeError(capability()["hint"])
        if state.get("pending"):
            raise NighttimeError("A wake test is already pending.")
        preflight()
        command("systemctl", "enable", "--now", TIMER)
        now = int(time.time())
        state.pop("test_candidate", None)
        state["pending"] = {"test": True, "phase": "countdown", "start": now + 120,
                            "wake": now + 420, "boot_id": boot_id(), "fingerprint": fingerprint()}
        save(state)
    return status()


def confirm_test(body):
    with locked():
        state = load()
        candidate = state.get("test_candidate")
        if (not isinstance(body, dict) or not candidate or body.get("id") != candidate["id"]
                or candidate["fingerprint"] != fingerprint() or time.time() - candidate["at"] > 86400):
            raise NighttimeError("No recent successful wake test to confirm.")
        state["verified"] = {"fingerprint": fingerprint(), "at": time.time()}
        state.pop("test_candidate")
        save(state)
    return status()


def environment():
    env = {}
    for line in read(ROOT / ".env").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip("'\"")
    return env


def containers():
    raw = command("docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "ps", "--all", "--format", "json")
    return json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line]


def healthy(expected=None):
    rows = {r["Service"]: r for r in containers()}
    env = environment()
    core = ["db", "redis", "caddy", "nextcloud", "homeassistant"]
    if env.get("VAULT_ENABLED", "true") == "true":
        core.append("vaultwarden")
    expected = expected or core
    bad = [name for name in expected if rows.get(name, {}).get("State") != "running"
           or rows.get(name, {}).get("Health") not in ("healthy", "")]
    if bad:
        raise NighttimeError("Services are not ready: " + ", ".join(bad))
    # Real HTTP readiness, including GPU model loading. No external WAN needed
    # for local services; configured remote access is checked separately.
    urls = [f"http://127.0.0.1:{env.get('NEXTCLOUD_PORT', '8080')}/status.php",
            f"http://127.0.0.1:{env.get('HA_PORT', '8123')}/"]
    if "vaultwarden" in expected:
        urls.append(f"http://127.0.0.1:{env.get('VAULT_PORT', '8082')}/alive")
    record = json.loads(read(ROOT / ".platform.json", "{}"))
    if record.get("has_gpu"):
        urls.append("http://127.0.0.1:8001/health")
    for url in urls:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise NighttimeError("An application is still starting.")
            if url.endswith("/status.php"):
                nc = json.load(response)
                if not nc.get("installed") or nc.get("maintenance") or nc.get("needsDbUpgrade"):
                    raise NighttimeError("Nextcloud is not ready for normal use.")
    command("systemctl", "is-active", "homebrain-manager.service")
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    from healthcheck import check_remote_access, check_openclaw_gateway
    for check in (check_remote_access(env), check_openclaw_gateway(bool(record.get("has_gpu")))):
        if check and check["level"] != "ok":
            raise NighttimeError(check["summary"])
    return [name for name, row in rows.items() if row.get("State") == "running"]


@contextmanager
def package_locks():
    # Keep these POSIX locks until the power controller exits: checking and
    # releasing would allow apt to start in the gap before shutdown.
    with ExitStack() as stack:
        for path in PACKAGE_LOCKS:
            if Path(path).exists():
                f = stack.enter_context(open(path, "a"))
                try:
                    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise NighttimeError("The package manager is busy.") from None
        yield


def preflight():
    if not (ROOT / ".setup_complete").exists() or (ROOT / ".restoring").exists():
        raise NighttimeError("Setup or restore has not finished.")
    if command("timedatectl", "show", "-p", "LocalRTC", "--value") != "no":
        raise NighttimeError("The hardware clock must use UTC before running a wake test.")
    if datetime.now(timezone.utc).year < 2026:
        raise NighttimeError("The system clock is not trustworthy.")
    alarm = read(RTC)
    if alarm and int(alarm) > time.time():
        raise NighttimeError("Another RTC wake alarm is already set.")
    task = json.loads(read(Path(tempfile.gettempdir()) / "homebrain_task_status.json", "{}"))
    if task.get("status") == "running":
        raise NighttimeError("A dashboard operation is running.")
    for unit in ("homebrain-update", "homebrain-backup", "homebrain-offsite", "homebrain-picture",
                 "homebrain-video", "apt-daily", "apt-daily-upgrade"):
        state = command("systemctl", "show", unit + ".service", "-p", "ActiveState", "--value")
        if state not in ("inactive", "failed"):
            raise NighttimeError(f"{unit} is running.")
    import media_jobs
    if media_jobs.active():
        raise NighttimeError("A media generation job is pending.")
    return healthy()


def clear_alarm(wake):
    if read(RTC) == str(wake):
        RTC.write_text("0")


def finish(state, result, message):
    state["last_result"] = {"result": result, "message": message, "at": int(time.time())}
    state.pop("pending", None)
    state.pop("skip_start", None)
    if result == "failed":
        state["config"] = (DEFAULT | state.get("config", {})) | {"enabled": False}
        state.pop("verified", None)
        state.pop("test_candidate", None)
    config = DEFAULT | state.get("config", {})
    state["next"] = next_cycle(config, time.time() + 60)
    state["boot_id"] = boot_id()
    save(state)


def tick():
    if not STATE.exists():
        return
    with locked():
        state = load()
        now = time.time()
        pending = state.get("pending")
        if pending and pending["phase"] == "poweroff":
            if pending["boot_id"] == boot_id():
                # Request was accepted but the machine never left this boot.
                if now < pending["start"] + 180:
                    return
                clear_alarm(pending.get("alarm", pending["wake"]))
                finish(state, "failed", "Shutdown did not complete; schedule paused.")
                return
            # Actual boot time, not timer execution time, determines punctuality.
            boot_at = now - float(read("/proc/uptime").split()[0])
            if abs(boot_at - pending["wake"]) > 120:
                clear_alarm(pending.get("alarm", pending["wake"]))
                finish(state, "failed", "The box started outside its wake window; schedule paused.")
                return
            try:
                healthy(pending["containers"])
            except (NighttimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                if now - boot_at < 600:
                    return
                finish(state, "failed", "Recovery failed; schedule paused. " + str(e)[:200])
                return
            clear_alarm(pending.get("alarm", pending["wake"]))
            if pending["test"]:
                state["test_candidate"] = {"id": str(pending["start"]), "fingerprint": pending["fingerprint"], "at": int(now)}
            finish(state, "recovered", "Services recovered. Confirm the test only if the box woke automatically and its fans stopped."
                   if pending["test"] else "HomeBrain started on time and its services recovered.")
            return
        if pending:
            if pending["boot_id"] != boot_id():
                finish(state, "cancelled", "Wake test cancelled by an intervening restart.")
                return
            cycle = pending
        else:
            config = DEFAULT | state.get("config", {})
            if not config["enabled"]:
                return
            if not trusted(state):
                finish(state, "failed", "Hardware or kernel changed; run a new wake test.")
                return
            if state.get("boot_id") != boot_id() or not state.get("next"):
                finish(state, "skipped", "Started while nighttime was scheduled; staying available.")
                return
            cycle = state["next"]
        if now < cycle["start"]:
            return
        if now - cycle["start"] > RETRY + 60 or state.get("skip_start") == cycle["start"]:
            finish(state, "skipped", "This nighttime interval was skipped.")
            return
        try:
            with locked("power"), locked("accounts"), package_locks():
                expected = preflight()
                if cycle["wake"] - time.time() < 180:
                    raise NighttimeError("Too little time remains to shut down and wake safely.")
                # Persist the intent before touching hardware. A controller crash
                # can then be reconciled without a second poweroff attempt.
                state["pending"] = dict(cycle, test=bool(pending), phase="poweroff", boot_id=boot_id(),
                                        fingerprint=fingerprint(), containers=expected)
                save(state)
                command("rtcwake", "--utc", "--mode", "no", "--time", str(cycle["wake"]))
                alarm = int(read(RTC, "0") or "0")
                # rtcwake compensates the RTC/system-clock offset. Allow its
                # rounding, then remember the exact alarm we own for cleanup.
                if abs(alarm - cycle["wake"]) > 2:
                    raise NighttimeError("RTC alarm readback did not match; staying awake.")
                state["pending"]["alarm"] = alarm
                save(state)
                # Normal system shutdown lets Docker stop containers without
                # marking unless-stopped containers as manually stopped.
                command("systemctl", "--check-inhibitors=yes", "poweroff")
                # Retain the barrier until systemd stops this controller.
                time.sleep(180)
                raise NighttimeError("Shutdown did not complete.")
        except (NighttimeError, OSError, ValueError, subprocess.SubprocessError) as e:
            attempted = state.get("pending", {}).get("phase") == "poweroff"
            if attempted:
                clear_alarm(state["pending"].get("alarm", cycle["wake"]))
            elif time.time() - cycle["start"] < RETRY:
                print(f"Nighttime waits: {e}", file=sys.stderr)
                return
            finish(state, "failed" if attempted else "skipped", str(e)[:300])


def reset():
    with locked():
        state = load()
        pending = state.get("pending")
        if pending and pending.get("phase") == "poweroff":
            clear_alarm(pending.get("alarm", pending["wake"]))
        save({"config": dict(DEFAULT)})


def wait_recovery():
    # A persistent backup timer may fire before the first nighttime recovery
    # probe. Don't let that backup stop HA/vault while recovery is being checked.
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        pending = load().get("pending", {})
        if pending.get("phase") != "poweroff" or pending.get("boot_id") == boot_id():
            return
        time.sleep(5)
    raise NighttimeError("Nighttime recovery has not completed; backup was not started.")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["reset"]:
            reset()
        elif sys.argv[1:] == ["wait-recovery"]:
            wait_recovery()
        elif sys.argv[1:] == ["tick"]:
            tick()
        else:
            raise NighttimeError("Use tick or reset.")
    except (NighttimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
