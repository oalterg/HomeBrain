#!/usr/bin/env python3
"""HomeBrain self-tool MCP server.

Lets OpenClaw operate the HomeBrain box itself — answer "are backups
working?", trigger a backup, restart a service, tail logs — all from
WhatsApp without bouncing through the dashboard browser UI.

Talks to the HomeBrain Flask dashboard over a Unix-domain socket OR over
HTTP localhost with a shared-secret token derived from MASTER_PASSWORD.
The plain HTTP path is the fallback when the dashboard does not yet
expose a Unix socket; either way the MCP server never holds long-lived
credentials in its own process — it pulls the bearer token from
HOMEBRAIN_SELF_TOKEN_FILE on every call.

Environment:
  HOMEBRAIN_BASE_URL    e.g. http://127.0.0.1:80 (default)
  HOMEBRAIN_SELF_TOKEN_FILE  path to bearer token (mode 0600)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

BASE_URL = os.environ.get("HOMEBRAIN_BASE_URL", "http://127.0.0.1:80").rstrip("/")
TOKEN_FILE = os.environ.get(
    "HOMEBRAIN_SELF_TOKEN_FILE",
    os.path.expanduser("~/.openclaw/homebrain.token"),
)


def _token() -> str:
    try:
        return open(TOKEN_FILE).read().strip()
    except OSError:
        return ""


def _http(method: str, path: str, body: dict | None = None,
          timeout: int = 10) -> tuple[int, dict | str]:
    tok = _token()
    if not tok:
        return 0, "no self-token configured"
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_service_status(_args: dict) -> dict:
    code, body = _http("GET", "/api/integrations/self/system-status")
    if code != 200:
        return unavailable(f"dashboard unreachable: {code}")
    return ok(**(body if isinstance(body, dict) else {"raw": body}))


def t_gpu_stats(_args: dict) -> dict:
    code, body = _http("GET", "/api/integrations/self/gpu")
    if code != 200:
        return unavailable("dashboard unreachable")
    return ok(**(body if isinstance(body, dict) else {}))


def t_logs_tail(args: dict) -> dict:
    target = (args.get("service") or "").strip()
    if not target:
        return err("service is required")
    code, body = _http("GET", f"/api/integrations/self/logs/{target}")
    if code != 200:
        return err(f"logs unavailable: {code}")
    text = body if isinstance(body, str) else json.dumps(body)
    return ok(service=target, lines=text.splitlines()[-200:])


def t_backup_now(args: dict) -> dict:
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = "HomeBrain: trigger a full backup now"
    if not confirm:
        action_id = Consent.issue("homebrain", summary, {}, chat_id, ttl=120)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "homebrain", chat_id)
    if redeemed is None:
        return err("confirmation_token invalid or expired")
    code, body = _http("POST", "/api/integrations/self/backup-now", {})
    if code not in (200, 202):
        return err(f"backup trigger failed: {code} {body}")
    audit("homebrain", "backup_now")
    return ok(triggered=True, hint="Watch /api/task_status for progress.")


def t_service_restart(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not name:
        return err("name is required")
    summary = f"HomeBrain: restart service '{name}'"
    if not confirm:
        action_id = Consent.issue("homebrain", summary, {"name": name},
                                  chat_id, ttl=120)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "homebrain", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    code, body = _http("POST", "/api/integrations/self/restart-service",
                       {"name": redeemed["name"]})
    if code != 200:
        return err(f"restart failed: {code} {body}")
    audit("homebrain", "service_restart", name=redeemed["name"])
    return ok(restarted=redeemed["name"])


def t_version(_args: dict) -> dict:
    code, body = _http("GET", "/api/integrations/self/version")
    if code != 200:
        return unavailable("dashboard unreachable")
    return ok(**(body if isinstance(body, dict) else {"raw": body}))


def t_integrations_status(_args: dict) -> dict:
    """Aggregate health of every wired-up integration. Lets the agent answer
    'is everything connected?' in one round-trip."""
    code, body = _http("GET", "/api/integrations/self/integrations")
    if code != 200:
        return unavailable("dashboard unreachable")
    return ok(**(body if isinstance(body, dict) else {}))


TOOLS = [
    {"name": "homebrain.service_status",
     "description": "Snapshot of HomeBrain service health (Nextcloud, HA, Vault, tunnel, etc.).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "homebrain.gpu_stats",
     "description": "Current GPU utilisation, VRAM use, and temperature.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "homebrain.logs_tail",
     "description": "Tail the last 200 log lines for one service.",
     "inputSchema": {"type": "object",
                     "properties": {"service": {"type": "string"}},
                     "required": ["service"]}},
    {"name": "homebrain.backup_now",
     "description": "ACT-tier: trigger a full backup. Requires consent token.",
     "inputSchema": {"type": "object",
                     "properties": {"confirmation_token": {"type": "string"}}}},
    {"name": "homebrain.service_restart",
     "description": "ACT-tier: restart a Docker service. Requires consent token.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "homebrain.version",
     "description": "Local version info and pending-update flag.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "homebrain.integrations_status",
     "description": "Aggregate connection status of all OpenClaw integrations.",
     "inputSchema": {"type": "object", "properties": {}}},
]


DISPATCH = {
    "homebrain.service_status": t_service_status,
    "homebrain.gpu_stats": t_gpu_stats,
    "homebrain.logs_tail": t_logs_tail,
    "homebrain.backup_now": t_backup_now,
    "homebrain.service_restart": t_service_restart,
    "homebrain.version": t_version,
    "homebrain.integrations_status": t_integrations_status,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-self", "0.1.0", TOOLS, dispatch)
