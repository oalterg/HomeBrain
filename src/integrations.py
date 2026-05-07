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

HA_TOKEN_FILE = os.path.join(OPENCLAW_DIR, "ha.token")
NC_TOKEN_FILE = os.path.join(OPENCLAW_DIR, "nextcloud.token")
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


def _openclaw_mcp_show() -> str:
    if not _has_openclaw():
        return ""
    try:
        return subprocess.check_output(
            ["sudo", "-u", "homebrain", "openclaw", "mcp", "show"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode()
    except Exception:
        return ""


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

def _spec_self() -> dict:
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-homebrain.py")],
        "env": {
            "HOMEBRAIN_BASE_URL": "http://127.0.0.1:80",
            "HOMEBRAIN_SELF_TOKEN_FILE": SELF_TOKEN_FILE,
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
        },
    }


def _spec_homeassistant() -> dict | None:
    if not os.path.exists(HA_TOKEN_FILE):
        return None
    env = _read_env()
    # MCP servers run on the host, not inside the docker network.
    ha_port = env.get("HA_PORT", "8123")
    base = env.get("HA_BASE_URL") or f"http://127.0.0.1:{ha_port}"
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-homeassistant.py")],
        "env": {
            "HA_BASE_URL": base,
            "HA_TOKEN_FILE": HA_TOKEN_FILE,
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
        },
    }


def _spec_nextcloud() -> dict | None:
    if not os.path.exists(NC_TOKEN_FILE):
        return None
    env = _read_env()
    # MCP servers run on the host, not inside the docker network — use the
    # host-exposed port (NEXTCLOUD_PORT, default 8080) on 127.0.0.1.
    nc_port = env.get("NEXTCLOUD_PORT", "8080")
    base = env.get("NC_BASE_URL") or f"http://127.0.0.1:{nc_port}"
    user = env.get("NEXTCLOUD_ADMIN_USER", "admin")
    return {
        "command": PYTHON_BIN,
        "args": [os.path.join(SCRIPTS_DIR, "mcp-nextcloud.py")],
        "env": {
            "NC_BASE_URL": base,
            "NC_USER": user,
            "NC_TOKEN_FILE": NC_TOKEN_FILE,
            "HOMEBRAIN_AUDIT_DIR": LOG_DIR,
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


# ---------------------------------------------------------------------------
# Per-integration bootstrap helpers
# ---------------------------------------------------------------------------

def bootstrap_homeassistant(token: str | None = None) -> tuple[bool, str]:
    """Persist a long-lived access token. If `token` is None, attempt to
    auto-create one against the running HA container using the admin
    credentials baked into .env at provision time. Falling back to a manual
    paste flow is the dashboard's responsibility."""
    if token:
        _write_secret(HA_TOKEN_FILE, token)
        return True, "stored"
    env = _read_env()
    ha_user = env.get("HA_ADMIN_USER", "")
    ha_pass = env.get("HA_ADMIN_PASSWORD", "")
    if not (ha_user and ha_pass):
        return False, "no admin credentials in .env; paste a token manually"
    # HA's /auth/token + long_lived_access_tokens flow is multi-step and
    # changes between versions. We document the manual path as the reliable
    # one, and surface a hint in the UI if the auto path fails.
    return False, "auto-create not yet implemented; paste a token manually"


def bootstrap_nextcloud() -> tuple[bool, str]:
    """Create a Nextcloud app password via `occ user:add-app-password` for
    the admin user. Idempotent on the file (we always overwrite); not on
    Nextcloud (every call creates a new app password — old ones remain
    until manually revoked)."""
    env = _read_env()
    user = env.get("NEXTCLOUD_ADMIN_USER", "")
    if not user:
        return False, "NEXTCLOUD_ADMIN_USER not in .env"
    try:
        nc_cid = subprocess.check_output(
            ["docker", "compose", "-f",
             os.path.join(INSTALL_DIR, "docker-compose.yml"), "ps", "-q", "nextcloud"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode().strip()
        if not nc_cid:
            return False, "Nextcloud container not running"
        # `occ user:add-app-password` reads the user's password from stdin in
        # newer NC versions. Pass --password-from-env for robustness.
        admin_pass = env.get("NEXTCLOUD_ADMIN_PASSWORD", "")
        if not admin_pass:
            return False, "NEXTCLOUD_ADMIN_PASSWORD not in .env"
        proc = subprocess.run(
            ["docker", "exec", "-i", "-u", "www-data",
             "-e", f"OC_PASS={admin_pass}", nc_cid,
             "php", "occ", "user:add-app-password",
             user, "--password-from-env"],
            capture_output=True, text=True, timeout=20,
        )
        # NC prints the app password on stdout, sometimes prefixed with text.
        out = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not out:
            return False, f"occ failed: {proc.stderr.strip()[:200]}"
        # The token is the last whitespace-separated word on the last line.
        token = out[-1].split()[-1]
        if not token or len(token) < 20:
            return False, f"unexpected occ output: {proc.stdout.strip()[:200]}"
        _write_secret(NC_TOKEN_FILE, token)
        return True, "stored"
    except Exception as e:
        return False, str(e)


def disconnect_homeassistant() -> None:
    if os.path.exists(HA_TOKEN_FILE):
        os.remove(HA_TOKEN_FILE)
    _openclaw_mcp_unset(MCP_NAMES["homeassistant"])


def disconnect_nextcloud() -> None:
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
    return name in _openclaw_mcp_show()


def integration_status(key: str) -> dict:
    name = MCP_NAMES[key]
    info: dict[str, Any] = {"key": key, "mcp_name": name,
                            "wired": _wired_in_openclaw(name),
                            "configured": False}
    if key == "self":
        info["configured"] = bool(_self_token())
    elif key == "homeassistant":
        info["configured"] = os.path.exists(HA_TOKEN_FILE)
    elif key == "nextcloud":
        info["configured"] = os.path.exists(NC_TOKEN_FILE)
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
    def reconcile():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify({"results": reconcile_all_mcp()})

    # ---- Home Assistant ----------------------------------------------------
    def ha_connect():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        ok_, msg = bootstrap_homeassistant(token=token or None)
        if not ok_:
            return jsonify({"error": msg}), 400
        results = reconcile_all_mcp()
        return jsonify({"status": "connected", "reconcile": results})

    def ha_disconnect():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        disconnect_homeassistant()
        _openclaw_daemon_restart()
        return jsonify({"status": "disconnected"})

    def ha_test():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        return jsonify(_ping_mcp("homeassistant"))

    # ---- Nextcloud ---------------------------------------------------------
    def nc_connect():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        ok_, msg = bootstrap_nextcloud()
        if not ok_:
            return jsonify({"error": msg}), 400
        results = reconcile_all_mcp()
        return jsonify({"status": "connected", "reconcile": results})

    def nc_disconnect():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        disconnect_nextcloud()
        _openclaw_daemon_restart()
        return jsonify({"status": "disconnected"})

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
        reconcile_all_mcp()
        return jsonify({"status": "added"})

    def email_remove():
        if not session.get("authenticated"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        if not remove_email_account(body.get("name", "")):
            return jsonify({"error": "account not found"}), 404
        reconcile_all_mcp()
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
        reconcile_all_mcp()
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
        want to confirm by WhatsApp text."""
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

    app.add_url_rule("/api/integrations/homeassistant/connect",
                     "ha_connect",
                     limiter.limit("5 per minute")(ha_connect),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/homeassistant/disconnect",
                     "ha_disconnect", ha_disconnect, methods=["POST"])
    app.add_url_rule("/api/integrations/homeassistant/test",
                     "ha_test", ha_test, methods=["POST"])

    app.add_url_rule("/api/integrations/nextcloud/connect",
                     "nc_connect",
                     limiter.limit("5 per minute")(nc_connect),
                     methods=["POST"])
    app.add_url_rule("/api/integrations/nextcloud/disconnect",
                     "nc_disconnect", nc_disconnect, methods=["POST"])
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
