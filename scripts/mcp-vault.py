#!/usr/bin/env python3
"""HomeBrain Vault MCP server — exposes the local Vaultwarden as an MCP tool
to OpenClaw (or any MCP-compatible agent).

Design tenets
-------------
* The LLM never sees the master password. Unlock is performed *out of band*
  by the dashboard's `/api/vault/unlock` endpoint, which runs `bw unlock`
  and persists the resulting session token at $VAULT_SESSION_FILE
  (mode 0600, owned by the calling user).
* This MCP server reads that session token at every call. If the file is
  missing or the token is stale (`bw status` returns "locked"), the tool
  returns an `unlocked: false` envelope rather than prompting — letting
  the agent decide how to ask the user.
* Only metadata is returned by `vault.search` (name, username, URI, login
  notes). Full secrets require explicit `vault.reveal` with a per-item ID,
  which logs the access to stderr (captured by journald).

Wire-up
-------
Run as a stdio subprocess from OpenClaw's MCP launcher. Example fragment
for ~/.openclaw/openclaw.json:

  "mcp": {
    "servers": [
      {
        "name": "homebrain-vault",
        "command": "/usr/bin/python3",
        "args": ["/opt/homebrain/scripts/mcp-vault.py"],
        "env": {
          "VAULT_URL": "https://vault.example.com",
          "VAULT_SESSION_FILE": "/home/homebrain/.openclaw/vault.session"
        }
      }
    ]
  }

Or run as a systemd unit (see config/openclaw-mcp-vault.service) if your
agent supports remote MCP via Unix socket.

Dependencies
------------
* Bitwarden CLI: `npm install -g @bitwarden/cli` (the `bw` binary)
* Python: stdlib only (no external packages required for the wire protocol).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any

VAULT_URL = os.environ.get("VAULT_URL", "")
VAULT_BW_BIN = os.environ.get("VAULT_BW_BIN", "bw")
VAULT_SESSION_FILE = os.environ.get(
    "VAULT_SESSION_FILE",
    os.path.expanduser("~/.openclaw/vault.session"),
)
VAULT_AUDIT_LOG = os.environ.get(
    "VAULT_AUDIT_LOG",
    "/var/log/homebrain/mcp-vault-audit.log",
)


def _audit(action: str, **kwargs: Any) -> None:
    """Append a one-line JSON record for every secret-touching call."""
    rec = {"ts": datetime.utcnow().isoformat() + "Z", "action": action}
    rec.update(kwargs)
    line = json.dumps(rec, default=str)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        os.makedirs(os.path.dirname(VAULT_AUDIT_LOG), exist_ok=True)
        with open(VAULT_AUDIT_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_session() -> str | None:
    try:
        with open(VAULT_SESSION_FILE) as f:
            tok = f.read().strip()
        return tok or None
    except OSError:
        return None


def _bw(*args: str, session: str | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    if VAULT_URL:
        env["BW_SERVE_URL"] = VAULT_URL  # bw config
    if session:
        env["BW_SESSION"] = session
    try:
        proc = subprocess.run(
            [VAULT_BW_BIN, *args],
            capture_output=True, text=True, env=env, timeout=15,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{VAULT_BW_BIN} not found — install `@bitwarden/cli`"
    except subprocess.TimeoutExpired:
        return 124, "", "bw command timed out"


def _is_unlocked() -> bool:
    session = _read_session()
    if not session:
        return False
    rc, out, _ = _bw("status", session=session)
    if rc != 0:
        return False
    try:
        return json.loads(out).get("status") == "unlocked"
    except json.JSONDecodeError:
        return False


# --- MCP wire protocol (JSON-RPC 2.0 over stdio, MCP 2025-06-18 spec) ---

TOOLS = [
    {
        "name": "vault.search",
        "description": (
            "Search the local HomeBrain Vault by free-text query. Returns "
            "metadata only (name, username, URI). No secrets are revealed. "
            "Returns {unlocked: false} if the vault is locked — ask the user "
            "to unlock it via the dashboard."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault.reveal",
        "description": (
            "Reveal the password for a single vault item by ID. Audited. "
            "Use sparingly and ONLY after the user has explicitly asked "
            "for the secret. Returns {unlocked: false} if locked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Vault item UUID"},
                "reason": {"type": "string", "description": "Why the agent needs this — written to the audit log"},
            },
            "required": ["item_id", "reason"],
        },
    },
    {
        "name": "vault.status",
        "description": "Check whether the vault is currently unlocked.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name: str, args: dict) -> dict:
    if name == "vault.status":
        return {"unlocked": _is_unlocked(), "url": VAULT_URL}

    session = _read_session()
    if not session or not _is_unlocked():
        return {"unlocked": False, "hint": "Ask the user to unlock the vault from the HomeBrain dashboard."}

    if name == "vault.search":
        q = args.get("query", "")
        rc, out, err = _bw("list", "items", "--search", q, session=session)
        if rc != 0:
            return {"error": err.strip() or "search failed"}
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return {"error": "could not parse bw output"}
        # Strip secrets — never return passwords from search.
        results = []
        for it in items:
            login = it.get("login") or {}
            uris = [u.get("uri", "") for u in (login.get("uris") or [])]
            results.append({
                "id": it.get("id"),
                "name": it.get("name"),
                "username": login.get("username"),
                "uris": uris,
                "folder_id": it.get("folderId"),
            })
        _audit("search", query=q, hits=len(results))
        return {"unlocked": True, "results": results}

    if name == "vault.reveal":
        item_id = args.get("item_id", "")
        reason = args.get("reason", "(unspecified)")
        rc, out, err = _bw("get", "item", item_id, session=session)
        if rc != 0:
            return {"error": err.strip() or "item not found"}
        try:
            item = json.loads(out)
        except json.JSONDecodeError:
            return {"error": "could not parse bw output"}
        login = item.get("login") or {}
        _audit("reveal", item_id=item_id, item_name=item.get("name"), reason=reason)
        return {
            "unlocked": True,
            "id": item.get("id"),
            "name": item.get("name"),
            "username": login.get("username"),
            "password": login.get("password"),
            "totp_seed_present": bool(login.get("totp")),
            "notes": item.get("notes"),
        }

    return {"error": f"unknown tool: {name}"}


def write_message(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve() -> None:
    """Tiny JSON-RPC 2.0 loop for MCP over stdio. Implements just enough of
    the spec for `initialize`, `tools/list`, `tools/call`."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "homebrain-vault", "version": "0.1.0"},
                },
            })
        elif method == "tools/list":
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS},
            })
        elif method == "tools/call":
            tool = params.get("name", "")
            args = params.get("arguments") or {}
            result = call_tool(tool, args)
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": "error" in result,
                },
            })
        elif method == "notifications/initialized":
            pass  # one-way notification, no response
        elif msg_id is not None:
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })


if __name__ == "__main__":
    serve()
