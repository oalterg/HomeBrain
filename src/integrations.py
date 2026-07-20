"""HomeBrain × OpenClaw integrations module.

Owns the dashboard side of every integration (Home Assistant, Nextcloud,
Vault, Email, Self):

  * Provisions per-integration credentials (HA LLAT, NC app password,
    Vault session token, Email account creds, Self bearer token) — all
    derived from MASTER_PASSWORD or scoped tokens generated against the
    upstream service.
  * Wires the corresponding MCP server into ~/.openclaw/openclaw.json
    via `openclaw mcp set <name> <json-spec>`. The reconciler is the only
    code that writes that file; manual edits are detected and warned but
    not silently overwritten.
  * Exposes `/api/integrations/<name>/<verb>` endpoints for the dashboard
    Connections card (status, connect, disconnect, test, logs).
  * Provides authenticated bearer-token endpoints under
    `/api/integrations/self/*` for the mcp-homebrain self-tool.

See INTEGRATIONS_PLAN.md for the full design and rationale.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

from flask import jsonify, request, session

# ---------------------------------------------------------------------------
# Constants — single source of truth for paths used by the MCP servers too.
# ---------------------------------------------------------------------------

INSTALL_DIR = "/opt/homebrain"
HOMEBRAIN_HOME = os.environ.get("HOMEBRAIN_HOME", "/home/homebrain")
OPENCLAW_DIR = os.path.join(HOMEBRAIN_HOME, ".openclaw")
LOG_DIR = "/var/log/homebrain"

# Legacy single-account files — kept for migration; new accounts live in
# the *_ACCOUNTS_FILE companions below as JSON arrays.
HA_TOKEN_FILE = os.path.join(OPENCLAW_DIR, "ha.token")
NC_TOKEN_FILE = os.path.join(OPENCLAW_DIR, "nextcloud.token")
HA_ACCOUNTS_FILE = os.path.join(OPENCLAW_DIR, "ha_accounts.json")
NC_ACCOUNTS_FILE = os.path.join(OPENCLAW_DIR, "nc_accounts.json")
EMAIL_ACCOUNTS_FILE = os.path.join(OPENCLAW_DIR, "email_accounts.json")
SELF_TOKEN_FILE = os.path.join(OPENCLAW_DIR, "homebrain.token")

PYTHON_BIN = "/usr/bin/python3"
SCRIPTS_DIR = os.path.join(INSTALL_DIR, "scripts")

# Names used in `openclaw mcp set <name>`. Keep stable — changing breaks
# OpenClaw's existing tool registrations.
MCP_NAMES = {
    "homeassistant": "homebrain-homeassistant",
    "nextcloud":     "homebrain-nextcloud",
    "vault":         "homebrain-vault",
    "email":         "homebrain-email",
    "self":          "homebrain-self",
}

# Order matters for the dashboard card.
INTEGRATION_ORDER = ["self", "homeassistant", "nextcloud", "vault", "email"]

# Channels that HomeBrain can link via the dashboard (separate from MCP
# integrations — channels are messaging bridges, not tool servers). Telegram
# only: it ships inside core OpenClaw, needs no plugin install, and covers
# the product need with one code path.
CHANNEL_ORDER = ["telegram"]
TELEGRAM_API_BASE = "https://api.telegram.org/bot"


# ---------------------------------------------------------------------------
# Helpers — env, files, derivation
# ---------------------------------------------------------------------------

def _ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        import pwd
        uid = pwd.getpwnam("homebrain").pw_uid
        os.chown(path, uid, uid)
        os.chmod(path, mode)
    except Exception:
        pass


def _write_secret(path: str, content: str) -> None:
    """Atomic-ish 0600 write owned by homebrain."""
    _ensure_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    try:
        import pwd
        uid = pwd.getpwnam("homebrain").pw_uid
        os.chown(path, uid, uid)
    except Exception:
        pass


def _read_secret(path: str) -> str:
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def _read_env() -> dict:
    env_file = os.path.join(INSTALL_DIR, ".env")
    out: dict[str, str] = {}
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k] = v.strip("'\"")
    except OSError:
        pass
    return out


def _master_password() -> str:
    return _read_env().get("MASTER_PASSWORD", "")


def _self_token() -> str:
    """Derive a stable bearer token for the self-MCP from MASTER_PASSWORD.
    Same pattern as the Vault admin token: HMAC the master password with a
    per-install nonce. The plain token is never written to .env; it is
    written to ~/.openclaw/homebrain.token (mode 0600) by ensure_self_token()."""
    mp = _master_password()
    nonce = _read_env().get("HOMEBRAIN_SELF_NONCE", "")
    if not mp or not nonce:
        return ""
    return hmac.new(nonce.encode(), mp.encode(), hashlib.sha256).hexdigest()


def _ensure_self_nonce() -> None:
    """Create HOMEBRAIN_SELF_NONCE in .env if missing. Idempotent."""
    env = _read_env()
    if env.get("HOMEBRAIN_SELF_NONCE"):
        return
    nonce = secrets.token_hex(16)
    env_file = os.path.join(INSTALL_DIR, ".env")
    try:
        with open(env_file, "a") as f:
            f.write(f"\nHOMEBRAIN_SELF_NONCE={nonce}\n")
    except OSError:
        pass


def _ensure_self_token() -> str:
    """Make sure ~/.openclaw/homebrain.token exists and matches the derivation."""
    _ensure_self_nonce()
    tok = _self_token()
    if not tok:
        return ""
    on_disk = _read_secret(SELF_TOKEN_FILE)
    if on_disk != tok:
        _write_secret(SELF_TOKEN_FILE, tok)
    return tok


def _email_fernet_key() -> str:
    """Derive a Fernet-format key from MASTER_PASSWORD. Stored on disk so
    the dashboard and the email MCP both reach the same value across
    restarts even if MASTER_PASSWORD is later rotated (rotation requires
    re-entering account passwords; that is by design)."""
    env = _read_env()
    existing = env.get("HOMEBRAIN_EMAIL_KEY")
    if existing:
        return existing
    # Use PBKDF2 to derive a 32-byte key, then base64 url-safe per Fernet.
    mp = _master_password()
    if not mp:
        return ""
    salt = b"homebrain-email-fernet-v1"
    raw = hashlib.pbkdf2_hmac("sha256", mp.encode(), salt, 200_000, dklen=32)
    key = base64.urlsafe_b64encode(raw).decode()
    # Persist so future MASTER_PASSWORD reads don't recompute (and so the
    # value survives a master-password rotation if the operator chooses
    # not to also rotate stored email account passwords).
    env_file = os.path.join(INSTALL_DIR, ".env")
    try:
        with open(env_file, "a") as f:
            f.write(f"\nHOMEBRAIN_EMAIL_KEY={key}\n")
    except OSError:
        pass
    return key


def _encrypt_secret(plain: str) -> str:
    key = _email_fernet_key()
    if not key:
        return plain  # fallback — dev only
    try:
        from cryptography.fernet import Fernet  # type: ignore
        return Fernet(key.encode()).encrypt(plain.encode()).decode()
    except Exception:
        return plain


# ---------------------------------------------------------------------------
# OpenClaw MCP wiring
# ---------------------------------------------------------------------------

def _has_openclaw() -> bool:
    return shutil.which("openclaw") is not None


# Wiring state lives in ~/.openclaw/openclaw.json under .mcp.servers — a
# plain key/value map of name → server-spec. The integrations status
# endpoint only needs to know which names are present, so we read the
# JSON directly instead of shelling out to `openclaw mcp show`.
#
# Why this matters: shelling to the CLI for a read every 15 s (×5
# integrations per /api/integrations/status request) was:
#   - 15-25 s per CLI invocation in practice (gets slower under load)
#   - leaking 6+ orphaned Node child processes per call (the CLI spawns
#     each MCP server to enumerate tools, and those children detach)
#   - the direct cause of two OOM cascades on .58 today
# A file read is sub-millisecond and has no side effects.
#
# We *do* still keep `_openclaw_mcp_set` / `_openclaw_mcp_unset`
# shelling out, because those need to invoke the CLI's atomic write
# path (config validation + reconciliation), and they only run on
# explicit user action — not on every status poll.
_OPENCLAW_CONFIG_PATH = os.path.join(OPENCLAW_DIR, "openclaw.json")


def _openclaw_wired_names() -> set[str]:
    """Read the set of currently-wired MCP server names from
    `~/.openclaw/openclaw.json`. Returns an empty set if the file is
    missing or unreadable — callers treat that as "nothing wired,"
    which matches the dashboard's existing semantics."""
    try:
        with open(_OPENCLAW_CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    servers = data.get("mcp", {}).get("servers", {})
    return set(servers.keys()) if isinstance(servers, dict) else set()


def _openclaw_mcp_set(name: str, spec: dict) -> tuple[bool, str]:
    if not _has_openclaw():
        return False, "openclaw CLI not found"
    try:
        subprocess.check_call(
            ["sudo", "-u", "homebrain", "openclaw", "mcp", "set",
             name, json.dumps(spec)],
            stderr=subprocess.STDOUT, timeout=15,
        )
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"openclaw mcp set failed: {e}"


def _openclaw_mcp_unset(name: str) -> None:
    if not _has_openclaw():
        return
    try:
        subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw", "mcp", "unset", name],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _openclaw_daemon_restart() -> None:
    if not _has_openclaw():
        return
    try:
        subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw", "daemon", "restart"],
            capture_output=True, timeout=20,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MCP server specs
# ---------------------------------------------------------------------------

def _mcp_consent_env() -> dict:
    """Return env vars that control MCP consent behavior.

    The HomeBrain MCP consent gate is disabled pending our upstream OpenClaw
    approvals PR. The flag used to live at mcp.approvals.enabled, but stock
    OpenClaw (>=2026.6) strictly validates the mcp section and rejects that
    key, so it is no longer persisted there. Until the upstream feature lands,
    consent is off (its long-standing default)."""
    return {"HOMEBRAIN_MCP_CONSENT": "false"}


def _spec_self() -> dict:
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-homebrain.py")],
        "env": {
            "HOMEBRAIN_BASE_URL": "http://127.0.0.1:80",
            "HOMEBRAIN_SELF_TOKEN_FILE": SELF_TOKEN_FILE,
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
            **_mcp_consent_env(),
        },
    }


def _spec_homeassistant() -> dict | None:
    # The HA MCP now reads a per-account list at runtime, so the spec
    # only points it at the file. Returning None when there are no
    # accounts leaves the integration "configured: false" in the UI.
    _migrate_legacy_ha_token()  # one-shot; cheap if already done
    if not _load_ha_accounts():
        return None
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-homeassistant.py")],
        "env": {
            "HA_ACCOUNTS_FILE": HA_ACCOUNTS_FILE,
            "HOMEBRAIN_INTEGRATIONS_KEY": _email_fernet_key(),
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
            **_mcp_consent_env(),
        },
    }


def _spec_nextcloud() -> dict | None:
    _migrate_legacy_nc_token()
    if not _load_nc_accounts():
        return None
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-nextcloud.py")],
        "env": {
            "NC_ACCOUNTS_FILE": NC_ACCOUNTS_FILE,
            "HOMEBRAIN_INTEGRATIONS_KEY": _email_fernet_key(),
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
            **_mcp_consent_env(),
        },
    }


def _spec_vault() -> dict | None:
    """Reuses the pattern already established by app.py's vault_mcp_wire_up.
    Wired only when an unlocked session file exists OR when the user
    explicitly clicks Connect — the existing endpoint handles unlock."""
    session_file = os.path.join(OPENCLAW_DIR, "vault.session")
    env = _read_env()
    public_url = env.get("VAULT_DOMAIN", "") or "http://127.0.0.1:8082"
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-vault.py")],
        "env": {
            "VAULT_URL": public_url,
            "VAULT_SESSION_FILE": session_file,
            "VAULT_AUDIT_LOG": os.path.join(LOG_DIR, "mcp-vault-audit.log"),
        },
    }


def _spec_email() -> dict | None:
    if not os.path.exists(EMAIL_ACCOUNTS_FILE):
        return None
    env = _read_env()
    send_direct = env.get("HOMEBRAIN_EMAIL_SEND_DIRECT", "false")
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-email.py")],
        "env": {
            "HOMEBRAIN_EMAIL_ACCOUNTS": EMAIL_ACCOUNTS_FILE,
            "HOMEBRAIN_EMAIL_KEY": _email_fernet_key(),
            "HOMEBRAIN_EMAIL_SEND_DIRECT": send_direct,
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
            **_mcp_consent_env(),
        },
    }


SPEC_BUILDERS = {
    "self":          _spec_self,
    "homeassistant": _spec_homeassistant,
    "nextcloud":     _spec_nextcloud,
    "vault":         _spec_vault,
    "email":         _spec_email,
}


def reconcile_all_mcp() -> dict:
    """Idempotent — push every wired integration's MCP spec into OpenClaw.
    Self is always wired. The rest are wired iff their credentials exist.

    Called at provision time (from `provision.sh` via app.py) and whenever
    the user clicks "Apply" on the Connections card."""
    _ensure_dir(OPENCLAW_DIR)
    _ensure_self_token()
    results = {}
    for key in INTEGRATION_ORDER:
        spec = SPEC_BUILDERS[key]()
        name = MCP_NAMES[key]
        if spec is None:
            _openclaw_mcp_unset(name)
            results[key] = "not_configured"
            continue
        ok, msg = _openclaw_mcp_set(name, spec)
        results[key] = "wired" if ok else f"failed: {msg}"
    _openclaw_daemon_restart()
    return results


def reconcile_one(key: str) -> dict:
    """Targeted reconcile for a single integration after an account-list
    mutation. Skips the heavy `openclaw mcp set` when the integration is
    already wired — the MCP server reads the accounts file fresh on each
    tool call, so adding/removing a non-first / non-last account never
    needs a registry change.

    Use this from /api/integrations/<k>/add and /remove instead of
    `reconcile_all_mcp()` — running the full sweep on every account
    mutation can blow past gunicorn's worker timeout (5 × CLI calls @
    1-5 s each + a daemon restart).
    """
    _ensure_dir(OPENCLAW_DIR)
    name = MCP_NAMES[key]
    spec = SPEC_BUILDERS[key]()
    wired = name in _openclaw_wired_names()
    if spec is None:
        if wired:
            _openclaw_mcp_unset(name)
            _openclaw_daemon_restart()
            return {key: "unwired"}
        return {key: "not_configured"}
    if not wired:
        ok, msg = _openclaw_mcp_set(name, spec)
        if ok:
            _openclaw_daemon_restart()
            return {key: "wired"}
        return {key: f"failed: {msg}"}
    return {key: "wired (no-op; file change visible to MCP on next call)"}


# ---------------------------------------------------------------------------
# Home Assistant + Nextcloud account stores (multi-account)
# ---------------------------------------------------------------------------
# Each integration keeps a JSON file under ~/.openclaw with a list of
# accounts. Tokens are Fernet-encrypted at rest using the same key as
# email account passwords (HOMEBRAIN_INTEGRATIONS_KEY in MCP env, aka
# HOMEBRAIN_EMAIL_KEY in .env — same value, name kept for compat). Users
# may add as many accounts as they want, each pointing at a different
# domain. The MCP server-side dispatches tools by `account` parameter.

def _load_ha_accounts() -> list[dict]:
    if not os.path.exists(HA_ACCOUNTS_FILE):
        return []
    try:
        with open(HA_ACCOUNTS_FILE) as f:
            d = json.load(f)
        return d.get("accounts", []) if isinstance(d, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_ha_accounts(accounts: list[dict]) -> None:
    blob = json.dumps({"accounts": accounts}, indent=2)
    _write_secret(HA_ACCOUNTS_FILE, blob)


def add_ha_account(name: str, base_url: str, token: str) -> tuple[bool, str]:
    if not all([name, base_url, token]):
        return False, "name, base_url, and token are required"
    accounts = _load_ha_accounts()
    if any(a.get("name") == name for a in accounts):
        return False, f"account '{name}' already exists"
    accounts.append({
        "name": name,
        "base_url": base_url.rstrip("/"),
        "token": _encrypt_secret(token),
    })
    _save_ha_accounts(accounts)
    return True, "added"


def remove_ha_account(name: str) -> bool:
    accounts = _load_ha_accounts()
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return False
    if new:
        _save_ha_accounts(new)
    else:
        os.remove(HA_ACCOUNTS_FILE)
    return True


def _load_nc_accounts() -> list[dict]:
    if not os.path.exists(NC_ACCOUNTS_FILE):
        return []
    try:
        with open(NC_ACCOUNTS_FILE) as f:
            d = json.load(f)
        return d.get("accounts", []) if isinstance(d, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_nc_accounts(accounts: list[dict]) -> None:
    blob = json.dumps({"accounts": accounts}, indent=2)
    _write_secret(NC_ACCOUNTS_FILE, blob)


def add_nc_account(name: str, base_url: str, user: str, token: str) -> tuple[bool, str]:
    if not all([name, base_url, user, token]):
        return False, "name, base_url, user, and token are required"
    accounts = _load_nc_accounts()
    if any(a.get("name") == name for a in accounts):
        return False, f"account '{name}' already exists"
    accounts.append({
        "name": name,
        "base_url": base_url.rstrip("/"),
        "user": user,
        "token": _encrypt_secret(token),
    })
    _save_nc_accounts(accounts)
    return True, "added"


def remove_nc_account(name: str) -> bool:
    accounts = _load_nc_accounts()
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return False
    if new:
        _save_nc_accounts(new)
    else:
        os.remove(NC_ACCOUNTS_FILE)
    return True


# --- Migration from legacy single-account token files ----------------------

def _migrate_legacy_ha_token() -> None:
    """If the legacy `~/.openclaw/ha.token` exists, fold it into the new
    accounts store as account 'home' (the user's primary HA install) and
    delete the file. One-shot; safe to call on every spec build."""
    if not os.path.exists(HA_TOKEN_FILE):
        return
    if _load_ha_accounts():
        # Already migrated or user added accounts manually; just drop the
        # stale token file so the migration check stops firing.
        try:
            os.remove(HA_TOKEN_FILE)
        except OSError:
            pass
        return
    try:
        token = open(HA_TOKEN_FILE).read().strip()
    except OSError:
        return
    env = _read_env()
    ha_port = env.get("HA_PORT", "8123")
    base = env.get("HA_BASE_URL") or f"http://127.0.0.1:{ha_port}"
    add_ha_account("home", base, token)
    try:
        os.remove(HA_TOKEN_FILE)
    except OSError:
        pass


def _migrate_legacy_nc_token() -> None:
    """Mirror of _migrate_legacy_ha_token for Nextcloud. The legacy
    single-account flow always pointed at the HomeBrain-shipped NC, so we
    name the migrated entry 'homebrain'."""
    if not os.path.exists(NC_TOKEN_FILE):
        return
    if _load_nc_accounts():
        try:
            os.remove(NC_TOKEN_FILE)
        except OSError:
            pass
        return
    try:
        token = open(NC_TOKEN_FILE).read().strip()
    except OSError:
        return
    env = _read_env()
    nc_port = env.get("NEXTCLOUD_PORT", "8080")
    base = env.get("NC_BASE_URL") or f"http://127.0.0.1:{nc_port}"
    user = env.get("NEXTCLOUD_ADMIN_USER", "admin")
    add_nc_account("homebrain", base, user, token)
    try:
        os.remove(NC_TOKEN_FILE)
    except OSError:
        pass


# --- HomeBrain-NC convenience bootstrap ------------------------------------
# Auto-creates an app password against the LOCAL HomeBrain-shipped Nextcloud
# and stores it as account 'homebrain'. Used by the dashboard's one-click
# "Add this HomeBrain's Nextcloud" button. External NCs go through
# add_nc_account directly with a user-supplied app password.

def _nc_container_id() -> str:
    """Resolve the running Nextcloud container's id, or '' if not running."""
    try:
        return subprocess.check_output(
            ["docker", "compose", "-f",
             os.path.join(INSTALL_DIR, "docker-compose.yml"), "ps", "-q", "nextcloud"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode().strip()
    except Exception:
        return ""


def list_local_nc_users() -> tuple[list[dict] | None, str]:
    """Enumerate on-device Nextcloud users via `occ user:list`, annotating
    which are already wired up as accounts so the dashboard can dim them.
    Returns ([{id, displayname, configured}], "") on success or (None, err)."""
    env = _read_env()
    if not env.get("NEXTCLOUD_ADMIN_USER"):
        return None, "NEXTCLOUD_ADMIN_USER not in .env"
    nc_cid = _nc_container_id()
    if not nc_cid:
        return None, "Nextcloud container not running"
    try:
        proc = subprocess.run(
            ["docker", "exec", "-u", "www-data", nc_cid,
             "php", "occ", "user:list", "--output=json"],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            return None, f"occ user:list failed: {proc.stderr.strip()[:200]}"
        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return None, f"could not list users: {e}"

    nc_port = env.get("NEXTCLOUD_PORT", "8080")
    base = (env.get("NC_BASE_URL") or f"http://127.0.0.1:{nc_port}").rstrip("/")
    taken = {(a.get("base_url", "").rstrip("/"), a.get("user"))
             for a in _load_nc_accounts()}
    users = [{"id": uid, "displayname": disp or uid,
              "configured": (base, uid) in taken}
             for uid, disp in data.items()]
    users.sort(key=lambda u: (u["configured"], u["id"]))
    return users, ""


def bootstrap_local_nextcloud(user: str | None = None,
                              password: str | None = None) -> tuple[bool, str]:
    """Mint an app password on the on-device NC and store it as an account.
    `user` defaults to NEXTCLOUD_ADMIN_USER; `password` defaults to
    NEXTCLOUD_ADMIN_PASSWORD when (and only when) targeting that admin —
    every other user must supply their own NC login password, which is
    used once to mint the app password and then discarded."""
    env = _read_env()
    admin_user = env.get("NEXTCLOUD_ADMIN_USER", "")
    if not admin_user:
        return False, "NEXTCLOUD_ADMIN_USER not in .env"
    target_user = (user or admin_user).strip()
    if not target_user:
        return False, "user is required"

    if password:
        target_pass = password
    elif target_user == admin_user:
        target_pass = env.get("NEXTCLOUD_ADMIN_PASSWORD", "")
        if not target_pass:
            return False, "NEXTCLOUD_ADMIN_PASSWORD not in .env"
    else:
        return False, f"password is required for non-admin user '{target_user}'"

    nc_port = env.get("NEXTCLOUD_PORT", "8080")
    base = (env.get("NC_BASE_URL") or f"http://127.0.0.1:{nc_port}").rstrip("/")
    existing = _load_nc_accounts()
    if any((a.get("base_url", "").rstrip("/") == base
            and a.get("user") == target_user) for a in existing):
        return False, f"local user '{target_user}' is already added"

    # Pick a unique account name. The username is the natural choice;
    # only fall back to a suffix on collision (e.g. an external NC also
    # named 'alice'). Legacy installs keep their 'homebrain' account.
    account_name = target_user
    taken_names = {a.get("name") for a in existing}
    if account_name in taken_names:
        i = 2
        while f"{account_name}-{i}" in taken_names:
            i += 1
        account_name = f"{account_name}-{i}"

    nc_cid = _nc_container_id()
    if not nc_cid:
        return False, "Nextcloud container not running"
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", "-u", "www-data",
             "-e", f"OC_PASS={target_pass}", nc_cid,
             "php", "occ", "user:add-app-password",
             target_user, "--password-from-env"],
            capture_output=True, text=True, timeout=20,
        )
        out = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not out:
            return False, f"occ failed: {proc.stderr.strip()[:200]}"
        token = out[-1].split()[-1]
        if not token or len(token) < 20:
            return False, f"unexpected occ output: {proc.stdout.strip()[:200]}"
        return add_nc_account(account_name, base, target_user, token)
    except Exception as e:
        return False, str(e)


def disconnect_homeassistant_all() -> None:
    if os.path.exists(HA_ACCOUNTS_FILE):
        os.remove(HA_ACCOUNTS_FILE)
    if os.path.exists(HA_TOKEN_FILE):
        os.remove(HA_TOKEN_FILE)
    _openclaw_mcp_unset(MCP_NAMES["homeassistant"])


def disconnect_nextcloud_all() -> None:
    if os.path.exists(NC_ACCOUNTS_FILE):
        os.remove(NC_ACCOUNTS_FILE)
    if os.path.exists(NC_TOKEN_FILE):
        os.remove(NC_TOKEN_FILE)
    _openclaw_mcp_unset(MCP_NAMES["nextcloud"])


# ---------------------------------------------------------------------------
# Email account store
# ---------------------------------------------------------------------------

def _load_email_accounts() -> list[dict]:
    if not os.path.exists(EMAIL_ACCOUNTS_FILE):
        return []
    try:
        with open(EMAIL_ACCOUNTS_FILE) as f:
            d = json.load(f)
        return d.get("accounts", []) if isinstance(d, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_email_accounts(accounts: list[dict]) -> None:
    blob = json.dumps({"accounts": accounts}, indent=2)
    _write_secret(EMAIL_ACCOUNTS_FILE, blob)


def add_email_account(name: str, user: str, imap_host: str, imap_port: int,
                      smtp_host: str, smtp_port: int, password: str) -> tuple[bool, str]:
    if not all([name, user, imap_host, smtp_host, password]):
        return False, "missing required fields"
    accounts = _load_email_accounts()
    if any(a.get("name") == name for a in accounts):
        return False, f"account '{name}' already exists"
    enc = _encrypt_secret(password)
    accounts.append({
        "name": name,
        "user": user,
        "imap_host": imap_host,
        "imap_port": int(imap_port or 993),
        "smtp_host": smtp_host,
        "smtp_port": int(smtp_port or 587),
        "imap_password": enc,
        "smtp_password": enc,
        "drafts_folder": "Drafts",
        "archive_folder": "Archive",
    })
    _save_email_accounts(accounts)
    return True, "added"


def remove_email_account(name: str) -> bool:
    accounts = _load_email_accounts()
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return False
    if new:
        _save_email_accounts(new)
    else:
        os.remove(EMAIL_ACCOUNTS_FILE)
    return True


# ---------------------------------------------------------------------------
# Status aggregation
# ---------------------------------------------------------------------------

def _wired_in_openclaw(name: str) -> bool:
    return name in _openclaw_wired_names()


def integration_status(key: str) -> dict:
    name = MCP_NAMES[key]
    info: dict[str, Any] = {"key": key, "mcp_name": name,
                            "wired": _wired_in_openclaw(name),
                            "configured": False}
    if key == "self":
        info["configured"] = bool(_self_token())
    elif key == "homeassistant":
        _migrate_legacy_ha_token()
        accounts = _load_ha_accounts()
        info["configured"] = bool(accounts)
        info["accounts"] = [{"name": a.get("name"), "base_url": a.get("base_url")}
                            for a in accounts]
    elif key == "nextcloud":
        _migrate_legacy_nc_token()
        accounts = _load_nc_accounts()
        info["configured"] = bool(accounts)
        # is_local lets the dashboard render local-vs-external accounts
        # differently (e.g. dim the "Add HomeBrain user" picker entries
        # that are already wired up). Match by base_url against .env.
        env = _read_env()
        nc_port = env.get("NEXTCLOUD_PORT", "8080")
        local_base = (env.get("NC_BASE_URL")
                      or f"http://127.0.0.1:{nc_port}").rstrip("/")
        info["accounts"] = [{"name": a.get("name"), "base_url": a.get("base_url"),
                             "user": a.get("user"),
                             "is_local": (a.get("base_url") or "").rstrip("/") == local_base}
                            for a in accounts]
    elif key == "vault":
        # Match the existing dashboard logic — vault is "configured" once
        # the admin token has been derived in .env.
        info["configured"] = bool(_read_env().get("VAULT_ADMIN_TOKEN"))
        info["unlocked"] = os.path.exists(
            os.path.join(OPENCLAW_DIR, "vault.session"))
    elif key == "email":
        accounts = _load_email_accounts()
        info["configured"] = bool(accounts)
        info["accounts"] = [{"name": a.get("name"), "user": a.get("user"),
                             "imap_host": a.get("imap_host")}
                            for a in accounts]
        info["send_direct_enabled"] = (
            _read_env().get("HOMEBRAIN_EMAIL_SEND_DIRECT", "false").lower() == "true"
        )
    return info


# ---------------------------------------------------------------------------
# Bearer-token auth (for the self-MCP)
# ---------------------------------------------------------------------------

def _check_bearer() -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    given = auth[len("Bearer "):].strip()
    expected = _self_token()
    if not expected:
        return False
    return hmac.compare_digest(given, expected)


def _require_session_or_bearer() -> bool:
    return session.get("authenticated") or _check_bearer()


# ---------------------------------------------------------------------------
# Channel linking — Telegram
# ---------------------------------------------------------------------------
# Channels are messaging bridges (not MCP tool servers). Their config lives
# directly in openclaw.json under .channels.<id> and .plugins.entries.<id>.
# We read/write the JSON file directly — channels don't need the heavy
# `openclaw mcp set` path.

import urllib.request
import urllib.error


def _read_openclaw_config() -> dict:
    try:
        with open(_OPENCLAW_CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_openclaw_channel(channel_id: str, channel_cfg: dict,
                            enable_plugin: bool = True) -> None:
    _ensure_dir(OPENCLAW_DIR)
    try:
        with open(_OPENCLAW_CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}

    channels = data.setdefault("channels", {})
    channels[channel_id] = channel_cfg

    if enable_plugin:
        plugins = data.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entries[channel_id] = {"enabled": True}

    # Sync allowFrom → commands.ownerAllowFrom so the user can run
    # /approve and other owner commands from the same channel.
    allow_from = channel_cfg.get("allowFrom", [])
    if allow_from:
        commands = data.setdefault("commands", {})
        existing = set(commands.get("ownerAllowFrom", []))
        existing.update(allow_from)
        commands["ownerAllowFrom"] = sorted(existing)

    with open(_OPENCLAW_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _remove_openclaw_channel(channel_id: str) -> None:
    try:
        with open(_OPENCLAW_CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    channels = data.get("channels", {})
    channels.pop(channel_id, None)
    plugins = data.get("plugins", {})
    entries = plugins.get("entries", {})
    entries.pop(channel_id, None)
    with open(_OPENCLAW_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _channel_status(channel_id: str) -> dict:
    data = _read_openclaw_config()
    ch = data.get("channels", {}).get(channel_id, {})
    plugin = data.get("plugins", {}).get("entries", {}).get(channel_id, {})
    configured = bool(ch)
    enabled = ch.get("enabled", False) and plugin.get("enabled", False)
    info: dict[str, Any] = {
        "key": channel_id,
        "configured": configured,
        "enabled": enabled,
    }
    if channel_id == "telegram" and configured:
        info["has_token"] = bool(ch.get("botToken"))
    return info


def _validate_telegram_token(token: str) -> dict:
    url = f"{TELEGRAM_API_BASE}{token}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        if body.get("ok") and body.get("result"):
            bot = body["result"]
            return {"ok": True,
                    "bot_name": bot.get("first_name", ""),
                    "bot_username": bot.get("username", "")}
        return {"ok": False, "error": body.get("description", "Unknown error")}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"ok": False, "error": body.get("description", f"HTTP {e.code}")}
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _clean_openclaw_error(stderr: str, stdout: str) -> str:
    """Distil a user-facing message from OpenClaw CLI error output.

    The CLI prints boilerplate around the actual cause, e.g.:
        [openclaw] Could not start the CLI.
        [openclaw] Reason: No pending pairing request found for code "X".
        [openclaw] Debug: set OPENCLAW_DEBUG=1 ...
        [openclaw] Try: openclaw doctor
    Prefer the "Reason:" line; otherwise fall back to the first meaningful
    line, then to the raw text.
    """
    text = (stderr or stdout or "").strip()
    for line in text.splitlines():
        line = line.strip()
        marker = "Reason:"
        if marker in line:
            return line.split(marker, 1)[1].strip()
    for line in text.splitlines():
        line = line.strip()
        if line and "Could not start the CLI" not in line:
            return line.removeprefix("[openclaw]").strip()
    return text or "Approval failed"


def _approve_pairing(channel: str, code: str) -> tuple[dict, int]:
    """Run `openclaw pairing approve <channel> <code>` as the homebrain user.

    Returns (json_body, http_status).
    """
    code = (code or "").strip().upper()
    if not code or not (4 <= len(code) <= 16) or not code.isalnum():
        return {"error": "Invalid pairing code"}, 400
    try:
        proc = subprocess.run(
            ["sudo", "-u", "homebrain", "openclaw",
             "pairing", "approve", channel, code, "--notify"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Pairing approval timed out"}, 504
    except Exception as e:  # pragma: no cover - defensive
        return {"error": str(e)}, 500
    if proc.returncode == 0:
        return {"status": "approved", "output": proc.stdout.strip()}, 200
    return {"error": _clean_openclaw_error(proc.stderr, proc.stdout)}, 400

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

def register_integrations(app, limiter) -> None:  # noqa: C901
    """Bolt every integration endpoint onto the existing Flask app.

    Called from src/app.py once at import time. We add routes via
    `app.add_url_rule` so we keep the existing single-file Flask app
    pattern without forcing a Blueprint refactor."""

    # ---- Status (session-auth) ---------------------------------------------
    def status_all():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify({
            "openclaw_available": _has_openclaw(),
            "integrations": [integration_status(k) for k in INTEGRATION_ORDER],
        })

    def status_self_bearer():
        """Same payload, but bearer-token-auth so the self-MCP can call it."""
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return jsonify({
            "openclaw_available": _has_openclaw(),
            "integrations": [integration_status(k) for k in INTEGRATION_ORDER],
        })

    # ---- Reconcile (push every spec into openclaw) ------------------------
    # Fire-and-forget. Each `openclaw mcp set` is a Node CLI invocation
    # taking ~5-10 s under 2026.5; multiplied across the 4-5 configured
    # integrations plus the daemon restart, the full sweep regularly
    # exceeds gunicorn's 30 s worker timeout and returns 500 even though
    # the underlying work succeeds. Async + immediate ack mirrors the
    # backup / restore pattern.
    def reconcile():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        threading.Thread(target=reconcile_all_mcp, daemon=True).start()
        return jsonify({"status": "started",
                        "message": "Reconcile running in the background."})

    # ---- Home Assistant (multi-account) ------------------------------------
    def ha_add():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        ok_, msg = add_ha_account(
            name=(body.get("name") or "").strip(),
            base_url=(body.get("base_url") or "").strip(),
            token=(body.get("token") or "").strip(),
        )
        if not ok_:
            return jsonify({"error": msg}), 400
        reconcile_one("homeassistant")
        return jsonify({"status": "added"})

    def ha_remove():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        if not remove_ha_account((body.get("name") or "").strip()):
            return jsonify({"error": "account not found"}), 404
        reconcile_one("homeassistant")
        return jsonify({"status": "removed"})

    def ha_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("homeassistant"))

    # ---- Nextcloud (multi-account) -----------------------------------------
    def nc_add():
        """Add an EXTERNAL Nextcloud account. For the HomeBrain-shipped NC,
        use nc_add_local which auto-creates an app password."""
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        ok_, msg = add_nc_account(
            name=(body.get("name") or "").strip(),
            base_url=(body.get("base_url") or "").strip(),
            user=(body.get("user") or "").strip(),
            token=(body.get("token") or "").strip(),
        )
        if not ok_:
            return jsonify({"error": msg}), 400
        reconcile_one("nextcloud")
        return jsonify({"status": "added"})

    def nc_add_local():
        """Mint an app password against the HomeBrain-shipped Nextcloud and
        store it as an account. With no body, defaults to admin (one-click
        legacy flow). Body `{user, password?}` targets any local user;
        admin's password is read from .env, everyone else must supply
        theirs (used once to mint the app password, never stored)."""
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        user = (body.get("user") or "").strip() or None
        password = body.get("password") or None
        ok_, msg = bootstrap_local_nextcloud(user=user, password=password)
        if not ok_:
            return jsonify({"error": msg}), 400
        reconcile_one("nextcloud")
        return jsonify({"status": "added"})

    def nc_local_users():
        """List on-device NC users so the dashboard can populate the
        "Add HomeBrain user" picker, marking which are already wired up."""
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        users, err_msg = list_local_nc_users()
        if users is None:
            return jsonify({"error": err_msg}), 400
        env = _read_env()
        admin_user = env.get("NEXTCLOUD_ADMIN_USER", "")
        return jsonify({"users": users, "admin_user": admin_user,
                        "admin_password_stored": bool(env.get("NEXTCLOUD_ADMIN_PASSWORD"))})

    def nc_remove():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        if not remove_nc_account((body.get("name") or "").strip()):
            return jsonify({"error": "account not found"}), 404
        reconcile_one("nextcloud")
        return jsonify({"status": "removed"})

    def nc_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("nextcloud"))

    def vault_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("vault"))

    def email_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("email"))

    def self_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("self"))

    # ---- Email -------------------------------------------------------------
    def email_add():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        ok_, msg = add_email_account(
            name=body.get("name", "").strip(),
            user=body.get("user", "").strip(),
            imap_host=body.get("imap_host", "").strip(),
            imap_port=int(body.get("imap_port") or 993),
            smtp_host=body.get("smtp_host", "").strip(),
            smtp_port=int(body.get("smtp_port") or 587),
            password=body.get("password", ""),
        )
        if not ok_:
            return jsonify({"error": msg}), 400
        reconcile_one("email")
        return jsonify({"status": "added"})

    def email_remove():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        if not remove_email_account(body.get("name", "")):
            return jsonify({"error": "account not found"}), 404
        reconcile_one("email")
        return jsonify({"status": "removed"})

    def email_send_direct_toggle():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        # Delegate to the same env-update helper app.py already uses; we
        # re-implement locally to avoid an import cycle.
        env_file = os.path.join(INSTALL_DIR, ".env")
        env = _read_env()
        env["HOMEBRAIN_EMAIL_SEND_DIRECT"] = "true" if enabled else "false"
        try:
            with open(env_file, "w") as f:
                for k, v in env.items():
                    f.write(f"{k}={v}\n")
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        # The toggle changes an env var that the email MCP server only
        # reads at spawn time, so bounce the daemon to re-spawn it. No
        # registry change needed — skip the full reconcile.
        _openclaw_daemon_restart()
        return jsonify({"status": "ok", "enabled": enabled})

    # ---- Self MCP — bearer-auth endpoints ---------------------------------
    def _proxy_view(view_name: str):
        """Re-invoke a session-protected view inside a fake authenticated
        request. The bearer token has already been verified by the caller."""
        from flask import current_app
        view = current_app.view_functions.get(view_name)
        if view is None:
            return jsonify({"error": f"view '{view_name}' not registered"}), 500
        # The view reads `session` directly; populate it for this call only.
        session["authenticated"] = True
        try:
            return view()
        finally:
            session.pop("authenticated", None)

    def self_status():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return _proxy_view("system_status")

    def self_gpu():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return _proxy_view("system_capabilities")

    def self_version():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return _proxy_view("check_manager_update")

    def self_backup_now():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return _proxy_view("trigger_backup")

    def self_logs(target):
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        from flask import current_app
        view = current_app.view_functions.get("get_logs")
        if view is None:
            return jsonify({"error": "logs view missing"}), 500
        session["authenticated"] = True
        try:
            return view(target)
        finally:
            session.pop("authenticated", None)

    def self_integrations_status():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        return jsonify({
            "openclaw_available": _has_openclaw(),
            "integrations": [integration_status(k) for k in INTEGRATION_ORDER],
        })

    def self_restart_service():
        if not _check_bearer():
            return jsonify({"error": "unauthorised"}), 401
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        # Whitelist what can be restarted via the agent. Critical infra
        # (db, the dashboard itself) is intentionally absent.
        allowed = {"nextcloud", "homeassistant", "vaultwarden", "caddy", "redis"}
        if name not in allowed:
            return jsonify({"error": f"service '{name}' is not restartable from the agent"}), 403
        try:
            subprocess.run(
                ["docker", "compose", "-f",
                 os.path.join(INSTALL_DIR, "docker-compose.yml"), "restart", name],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        logging.info("Self-MCP restarted service: %s", name)
        return jsonify({"status": "restarted", "service": name})

    # ---- Pending consent inspection (for dashboard) -----------------------
    def pending_actions():
        """List currently outstanding consent tokens. The dashboard surfaces
        these so a user can approve/deny from the browser when they don't
        want to confirm by chat message."""
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        path = os.path.join(OPENCLAW_DIR, "pending_actions.json")
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return jsonify({"actions": []})
        now = time.time()
        out = []
        for aid, rec in (data or {}).items():
            if rec.get("expires_at", 0) <= now:
                continue
            out.append({"action_id": aid, "server": rec.get("server"),
                        "summary": rec.get("summary"),
                        "expires_at": rec.get("expires_at"),
                        "issued_at": rec.get("issued_at")})
        return jsonify({"actions": out})

    # ---- Audit log tail ---------------------------------------------------
    def audit_tail(server: str):
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        if server not in {"homeassistant", "nextcloud", "vault", "email", "homebrain"}:
            return jsonify({"error": "unknown server"}), 404
        path = os.path.join(LOG_DIR, f"mcp-{server}-audit.log")
        if not os.path.exists(path):
            return jsonify({"lines": []})
        try:
            with open(path) as f:
                lines = f.readlines()[-200:]
        except OSError:
            lines = []
        return jsonify({"lines": [ln.strip() for ln in lines]})

    # ---- Wire up Flask URL rules ------------------------------------------
    app.add_url_rule("/api/integrations/status", "integrations_status",
                     status_all, methods=["GET"])
    app.add_url_rule("/api/integrations/self/status",
                     "integrations_self_status",
                     status_self_bearer, methods=["GET"])
    app.add_url_rule("/api/integrations/reconcile",
                     "integrations_reconcile",
                     limiter.limit("10 per minute")(reconcile),
                     methods=["POST"])

    app.add_url_rule("/api/integrations/homeassistant/add",
                     "ha_add",
                     limiter.limit("10 per minute")(ha_add),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/homeassistant/remove",
                     "ha_remove", ha_remove, methods=["POST"])
    app.add_url_rule("/api/integrations/homeassistant/test",
                     "ha_test", ha_test, methods=["POST"])

    app.add_url_rule("/api/integrations/nextcloud/add",
                     "nc_add",
                     limiter.limit("10 per minute")(nc_add),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/nextcloud/add_local",
                     "nc_add_local",
                     limiter.limit("5 per minute")(nc_add_local),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/nextcloud/local_users",
                     "nc_local_users", nc_local_users, methods=["GET"])
    app.add_url_rule("/api/integrations/nextcloud/remove",
                     "nc_remove", nc_remove, methods=["POST"])
    app.add_url_rule("/api/integrations/nextcloud/test",
                     "nc_test", nc_test, methods=["POST"])
    app.add_url_rule("/api/integrations/vault/test",
                     "vault_ping_test", vault_test, methods=["POST"])
    app.add_url_rule("/api/integrations/email/test",
                     "email_test", email_test, methods=["POST"])
    app.add_url_rule("/api/integrations/self/test",
                     "self_test", self_test, methods=["POST"])

    app.add_url_rule("/api/integrations/email/add",
                     "email_add",
                     limiter.limit("5 per minute")(email_add),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/email/remove",
                     "email_remove", email_remove, methods=["POST"])
    app.add_url_rule("/api/integrations/email/send-direct-toggle",
                     "email_send_direct_toggle",
                     email_send_direct_toggle, methods=["POST"])

    app.add_url_rule("/api/integrations/self/restart-service",
                     "self_restart_service", self_restart_service,
                     methods=["POST"])
    app.add_url_rule("/api/integrations/self/integrations",
                     "self_integrations_status",
                     self_integrations_status, methods=["GET"])
    app.add_url_rule("/api/integrations/self/system-status",
                     "self_system_status",
                     self_status, methods=["GET"])
    app.add_url_rule("/api/integrations/self/gpu",
                     "self_gpu", self_gpu, methods=["GET"])
    app.add_url_rule("/api/integrations/self/version",
                     "self_version_proxy", self_version, methods=["GET"])
    app.add_url_rule("/api/integrations/self/backup-now",
                     "self_backup_now", self_backup_now, methods=["POST"])
    app.add_url_rule("/api/integrations/self/logs/<target>",
                     "self_logs", self_logs, methods=["GET"])

    app.add_url_rule("/api/integrations/pending-actions",
                     "integrations_pending_actions",
                     pending_actions, methods=["GET"])
    app.add_url_rule("/api/integrations/audit/<server>",
                     "integrations_audit_tail",
                     audit_tail, methods=["GET"])

    # ---- Channel linking (Telegram) ---------------------------------------

    def channels_status():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify({
            "channels": [_channel_status(k) for k in CHANNEL_ORDER],
        })

    def telegram_add():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        if not token:
            return jsonify({"error": "Bot token is required"}), 400
        validation = _validate_telegram_token(token)
        if not validation["ok"]:
            return jsonify({"error": validation["error"]}), 400
        _write_openclaw_channel("telegram", {
            "enabled": True,
            "botToken": token,
            "dmPolicy": "pairing",
        })
        _openclaw_daemon_restart()
        return jsonify({
            "status": "linked",
            "bot_name": validation.get("bot_name", ""),
            "bot_username": validation.get("bot_username", ""),
        })

    def telegram_pair():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        payload, status = _approve_pairing("telegram", body.get("code", ""))
        return jsonify(payload), status

    def telegram_remove():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        _remove_openclaw_channel("telegram")
        _openclaw_daemon_restart()
        return jsonify({"status": "removed"})

    app.add_url_rule("/api/channels/status",
                     "channels_status", channels_status, methods=["GET"])
    app.add_url_rule("/api/channels/telegram/add",
                     "telegram_add",
                     limiter.limit("5 per minute")(telegram_add),
                     methods=["POST"])
    app.add_url_rule("/api/channels/telegram/pair",
                     "telegram_pair",
                     limiter.limit("10 per minute")(telegram_pair),
                     methods=["POST"])
    app.add_url_rule("/api/channels/telegram/remove",
                     "telegram_remove", telegram_remove, methods=["POST"])

    # Post-provision self-heal: reconcile_all_mcp() was historically only
    # triggered by the user clicking "Apply" in Connections, which left
    # self-MCP + vault-MCP unwired on fresh installs and after upgrades that
    # introduced new INTEGRATION_ORDER entries. Now we run it on app boot when
    # the registry is missing the canonical self-MCP entry. Multi-worker safe
    # via an exclusive flock on a tmpfs lockfile — at most one worker per host
    # does the work, the rest no-op. Idempotent on subsequent boots once the
    # self-MCP is wired.
    threading.Thread(target=_startup_wire_if_needed, daemon=True).start()


def _startup_wire_if_needed() -> None:
    if not _has_openclaw():
        return
    try:
        if _self_token() and _wired_in_openclaw(MCP_NAMES["self"]):
            return
    except Exception:
        pass
    import fcntl
    lock_path = "/tmp/homebrain-startup-reconcile.lock"
    try:
        fd = open(lock_path, "w")
    except OSError:
        return
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Re-check inside the lock to avoid a redundant daemon restart if a
        # sibling worker just finished.
        if _self_token() and _wired_in_openclaw(MCP_NAMES["self"]):
            return
        logging.info("startup self-heal: wiring missing MCP servers")
        try:
            reconcile_all_mcp()
        except Exception as e:
            logging.warning("startup reconcile failed: %s", e)
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


# ---------------------------------------------------------------------------
# Smoke test against an MCP server (used by integration_test endpoints)
# ---------------------------------------------------------------------------

def _ping_mcp(key: str) -> dict:
    """Spawn the MCP server, send tools/list, return tool count or error."""
    spec = SPEC_BUILDERS[key]()
    if spec is None:
        return {"ok": False, "error": "not configured"}
    cmd = [spec["command"]] + spec["args"]
    env = {**os.environ, **(spec.get("env") or {})}
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    try:
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        proc.stdin.write("\n".join(json.dumps(m) for m in msgs) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        # Read up to two responses (initialize + tools/list)
        out_lines = []
        deadline = time.time() + 5
        while time.time() < deadline and len(out_lines) < 2:
            line = proc.stdout.readline()
            if not line:
                break
            out_lines.append(line)
        proc.terminate()
        proc.wait(timeout=2)
        for line in out_lines:
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            res = resp.get("result", {})
            if "tools" in res:
                return {"ok": True, "tool_count": len(res["tools"]),
                        "tools": [t["name"] for t in res["tools"]]}
        return {"ok": False, "error": "no tools/list response"}
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
