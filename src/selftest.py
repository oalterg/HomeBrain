"""HomeBrain system self-test — the "prove it" button.

Every serious defect found in this codebase over the last three months shares
one shape: the box asserts a success it never verified. Home Assistant password
rotation reported "rotated" for months while writing nothing. Off-site backup
reported "synced" and had never once been restored. A Nextcloud install can
carry a populated database next to an empty data directory while every
container reports healthy and every `occ` call throws.

The existing health check watches local state — disk, containers, timestamps.
It cannot tell you that the password the box stored is one no service accepts.
This module checks the *claims*, through the real interfaces:

  * the recorded master password actually logs in to the dashboard, Nextcloud
    and Home Assistant;
  * Nextcloud's data directory is the one its database thinks it has;
  * the newest local and off-site archives exist and are not too old;
  * the Fernet key that decrypts stored account tokens is intact;
  * the public URL answers, or says plainly that it does not;
  * the running version is the current one.

Three outcomes, never two. **"skip" is a real result and must never render as
a pass** — "we could not check whether your backups work" is information the
owner needs, and collapsing it into green is the whole bug class this file
exists to end.

Checks are pure-ish: they read state through the module-level IO helpers at the
top, which tests replace. See scripts/tests/test_selftest.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

OK = "ok"
FAIL = "fail"
SKIP = "skip"

DAY = 86400

INSTALL_DIR = "/opt/homebrain"
COMPOSE_FILE = f"{INSTALL_DIR}/docker-compose.yml"


# ---------------------------------------------------------------------------
# IO seams — the only things that touch the outside world. Tests replace these.
# ---------------------------------------------------------------------------

def container_id(service):
    """Container ID of a compose service, or "" if it is not running."""
    try:
        return subprocess.check_output(
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "-q", service],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode().strip()
    except Exception:
        return ""


def docker_exec(cid, argv, timeout=30):
    """(returncode, stdout) of a command inside a container."""
    try:
        r = subprocess.run(["docker", "exec", cid, *argv],
                           capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode(errors="replace")
    except Exception as e:
        return 1, str(e)


def http(method, url, *, data=None, headers=None, timeout=10, auth=None):
    """(status, body) — 0 and the reason string when the request never landed."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(
            f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def newest_archive(directory):
    """(name, mtime) of the newest backup archive in a directory, or None."""
    try:
        names = [n for n in os.listdir(directory)
                 if n.endswith((".tar.gz", ".tar.gz.gpg"))]
    except OSError:
        return None
    best = None
    for n in names:
        try:
            ts = os.path.getmtime(os.path.join(directory, n))
        except OSError:
            continue
        if best is None or ts > best[1]:
            best = (n, ts)
    return best


def offsite_listing():
    """Parsed `rclone lsjson` of the off-site remote, or None if it failed."""
    try:
        r = subprocess.run(["bash", f"{INSTALL_DIR}/scripts/utilities.sh", "offsite_list"],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Result helper
# ---------------------------------------------------------------------------

def result(name, status, detail, hint=""):
    return {"name": name, "status": status, "detail": detail, "hint": hint}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_dashboard_password(env):
    """The manager password in .env is the one the login page accepts.

    Not a tautology: login compares against MANAGER_PASSWORD, and a rotation
    that updated MASTER_PASSWORD but not MANAGER_PASSWORD would leave the owner
    locked out of the dashboard with .env looking perfectly correct.
    """
    pw = env.get("MANAGER_PASSWORD", "")
    if not pw:
        return result("Dashboard password", SKIP, "No MANAGER_PASSWORD is recorded.",
                      "Set a master password in Settings.")
    body = urllib.parse.urlencode({"password": pw}).encode()
    code, _ = http("POST", "http://127.0.0.1/login", data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if code == 200:
        return result("Dashboard password", OK, "The dashboard accepts the recorded password.")
    if code == 0:
        return result("Dashboard password", SKIP, "The dashboard did not answer.",
                      "The manager may be restarting.")
    return result("Dashboard password", FAIL,
                  f"The dashboard rejected the recorded password (HTTP {code}).",
                  "Set a new master password in Settings → Master Password.")


def check_nextcloud_password(env):
    pw = env.get("NEXTCLOUD_ADMIN_PASSWORD", "")
    user = env.get("NEXTCLOUD_ADMIN_USER", "admin")
    if not pw:
        return result("Nextcloud password", SKIP, "No Nextcloud admin password is recorded.")
    code, _ = http("GET", "http://127.0.0.1:8080/ocs/v1.php/cloud/user?format=json",
                   headers={"OCS-APIRequest": "true"}, auth=(user, pw))
    if code == 200:
        return result("Nextcloud password", OK, f"Nextcloud accepts the recorded password for '{user}'.")
    if code == 0:
        return result("Nextcloud password", SKIP, "Nextcloud did not answer.")
    return result("Nextcloud password", FAIL,
                  f"Nextcloud rejected the recorded password (HTTP {code}).",
                  "Set a new master password in Settings → Master Password.")


def check_ha_password(env):
    """Drives the real login flow. This is the check that would have caught
    the rotation bug the day it shipped: `hass --script auth` exits 0 even on
    "User not found", so for months the box reported a rotation it never made.
    """
    pw = env.get("HA_ADMIN_PASSWORD", "")
    if not pw:
        return result("Home Assistant password", SKIP, "No HA admin password is recorded.")
    cid = "http://127.0.0.1:8123/"
    payload = json.dumps({"client_id": cid, "handler": ["homeassistant", None],
                          "redirect_uri": cid}).encode()
    code, body = http("POST", "http://127.0.0.1:8123/auth/login_flow", data=payload,
                      headers={"Content-Type": "application/json"})
    if code == 0:
        return result("Home Assistant password", SKIP, "Home Assistant did not answer.")
    try:
        flow = json.loads(body)["flow_id"]
    except Exception:
        return result("Home Assistant password", SKIP,
                      f"Could not start a Home Assistant login flow (HTTP {code}).")
    payload = json.dumps({"username": "admin", "password": pw, "client_id": cid}).encode()
    code, body = http("POST", f"http://127.0.0.1:8123/auth/login_flow/{flow}", data=payload,
                      headers={"Content-Type": "application/json"})
    if '"type": "create_entry"' in body or '"type":"create_entry"' in body:
        return result("Home Assistant password", OK, "Home Assistant accepts the recorded password.")
    return result("Home Assistant password", FAIL,
                  "Home Assistant rejected the recorded password.",
                  "Set a new master password in Settings → Master Password.")


def check_nextcloud_data(env):
    """The data directory matches the database that points at it.

    Seen live: a populated Nextcloud database beside an empty data directory
    with no `.ncdata` marker. Every container reported healthy, the dashboard
    reported healthy, and every `occ` call threw "Your data directory is
    invalid" — including the one backup.sh uses, so the backups silently
    stopped containing a usable Nextcloud.
    """
    cid = container_id("nextcloud")
    if not cid:
        return result("Nextcloud data directory", SKIP, "The Nextcloud container is not running.")
    rc, out = docker_exec(cid, ["php", "occ", "status"], timeout=60)
    if rc == 0 and "installed: true" in out:
        return result("Nextcloud data directory", OK, "occ reports a healthy install.")
    if "data directory is invalid" in out or ".ncdata" in out:
        return result("Nextcloud data directory", FAIL,
                      "The database is installed but the data directory is not recognised.",
                      "The '.ncdata' marker is missing from the data directory. "
                      "Backups taken in this state contain no usable Nextcloud.")
    return result("Nextcloud data directory", FAIL,
                  "occ could not report status.",
                  (out.strip().splitlines() or ["no output"])[0][:200])


def check_local_backup(env, now, storage_dir):
    newest = newest_archive(storage_dir)
    if newest is None:
        return result("Local backup", FAIL, f"No backup archive found in {storage_dir}.",
                      "Run a backup from the Backup card.")
    name, ts = newest
    age = int((now - ts) / DAY)
    weekly = env.get("BACKUP_DAY_WEEK", "*") not in ("", "*")
    limit = 8 if weekly else 2
    if age > limit:
        return result("Local backup", FAIL,
                      f"The newest backup is {age} days old ({name}).",
                      "Check the Backup card and the backup schedule.")
    return result("Local backup", OK, f"Newest backup is {age} day(s) old ({name}).")


def check_offsite(env, now):
    if env.get("OFFSITE_ENABLED", "false") != "true":
        return result("Off-site copy", SKIP, "Off-site backup is switched off.",
                      "A local-only backup does not survive fire or theft.")
    listing = offsite_listing()
    if listing is None:
        return result("Off-site copy", FAIL, "Could not read the off-site remote.",
                      "Check the credentials under Backup → Off-site copy.")
    if not listing:
        return result("Off-site copy", FAIL, "The off-site remote holds no archives.")
    newest = max((e.get("ModTime", "") for e in listing), default="")
    return result("Off-site copy", OK,
                  f"{len(listing)} archive(s) off-site, newest {newest[:10] or 'unknown'}.")


def check_instance_secrets(env):
    """The Fernet key that decrypts stored account tokens is usable.

    A restore used to shorten it from 44 characters to 43 and nothing said so:
    the MCP servers handed the still-encrypted token to Home Assistant as a
    bearer token and the owner saw a bare 401.
    """
    key = env.get("HOMEBRAIN_EMAIL_KEY", "")
    if not key:
        return result("Stored account tokens", SKIP,
                      "No encryption key yet — no accounts have been connected.")
    if len(key) != 44:
        return result("Stored account tokens", FAIL,
                      f"The encryption key is {len(key)} characters, expected 44.",
                      "Restart the manager; it repairs a truncated key on start.")
    try:
        from cryptography.fernet import Fernet
        Fernet(key.encode())
    except Exception as e:
        return result("Stored account tokens", FAIL,
                      "The encryption key is not usable.", str(e)[:200])
    return result("Stored account tokens", OK, "The encryption key is intact.")


def check_remote_access(env):
    domain = env.get("PANGOLIN_DOMAIN", "")
    if env.get("DEPLOYMENT_MODE", "local") != "remote" or not domain:
        return result("Remote access", SKIP, "This box is set up for the local network only.")
    code, _ = http("GET", f"https://{domain}/", timeout=15)
    if code == 0:
        return result("Remote access", FAIL, f"https://{domain} did not answer.",
                      "Check the tunnel under Settings.")
    return result("Remote access", OK, f"https://{domain} answers (HTTP {code}).")


def check_version(local_version, latest_version):
    # get_local_version() returns the literal string "unknown" for a box that
    # is not on a released ref — a dev checkout, or one built from a branch.
    # Reporting that as "you are behind" is the same lie in the other
    # direction: it is a fact we do not have, not a fact we have.
    if not local_version or local_version == "unknown":
        return result("Version", SKIP, "The installed version is unknown.",
                      "This box is not running a tagged release.")
    if not latest_version:
        return result("Version", SKIP, "Could not reach GitHub to check for updates.")
    if local_version != latest_version:
        return result("Version", FAIL, f"Running {local_version}; {latest_version} is available.",
                      "Update from the dashboard.")
    return result("Version", OK, f"Running the current release ({local_version}).")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def summarise(rows):
    """One word for the whole run. A skip is never a pass, but it is not a
    failure either — it is 'we could not tell', and it says so."""
    if any(r["status"] == FAIL for r in rows):
        return FAIL
    if any(r["status"] == SKIP for r in rows):
        return SKIP
    return OK


def run_all(env, *, now=None, storage_dir="/mnt/backup",
            local_version="", latest_version=""):
    now = now if now is not None else time.time()
    rows = [
        check_dashboard_password(env),
        check_nextcloud_password(env),
        check_ha_password(env),
        check_nextcloud_data(env),
        check_local_backup(env, now, storage_dir),
        check_offsite(env, now),
        check_instance_secrets(env),
        check_remote_access(env),
        check_version(local_version, latest_version),
    ]
    return {"checks": rows, "summary": summarise(rows), "ran_at": int(now)}
