"""Shared base library for HomeBrain MCP servers.

Every HomeBrain integration (Home Assistant, Nextcloud, Vault, Email, Self)
runs as a stdio MCP subprocess of the OpenClaw daemon. They all speak the
same JSON-RPC 2.0 wire protocol, return envelopes shaped the same way, and
share the same chat-message consent loop for `act`-tier tools.

This module factors out everything that would otherwise be copy-pasted into
five servers:

* `serve(server_name, version, tools, dispatch)` — the JSON-RPC stdio loop.
* `audit(server, action, **fields)` — append-only one-line JSON to the
  per-server audit log under /var/log/homebrain.
* `Consent` — the pending-actions store. An MCP server returns
  `consent_required(...)` for any `act`-tier tool; the agent re-calls with
  `confirmation_token=...` and `Consent.verify` lets the call through.
* `Envelope` helpers — `ok()`, `err()`, `unavailable()`, `locked()`.

See INTEGRATIONS_PLAN.md for the rationale.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

# Handler envelopes may stash extra MCP content blocks under this key.
# `tool_call_result` lifts them onto the wire and strips them from the
# JSON the model reads, so a camera JPEG does not get base64'd twice.
MCP_CONTENT = "_mcp_content"
MCP_MEDIA_PATH = "_mcp_media_path"

# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

def ok(**payload: Any) -> dict:
    return {"ok": True, **payload}

def err(message: str, **payload: Any) -> dict:
    return {"ok": False, "error": message, **payload}

def unavailable(hint: str = "") -> dict:
    return {"ok": False, "unavailable": True, "hint": hint}

def locked(hint: str = "") -> dict:
    return {"ok": False, "locked": True, "hint": hint}

def consent_required(action_id: str, summary: str, expires_in: int = 60) -> dict:
    return {
        "ok": False,
        "requires_confirmation": True,
        "action_id": action_id,
        "summary": summary,
        "expires_in_seconds": expires_in,
    }


def mcp_image(data: bytes, mime_type: str = "image/jpeg") -> dict:
    """An MCP image content block (SDK 1.29: top-level data + mimeType)."""
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime_type,
    }


def tool_call_result(result: dict) -> dict:
    """Shape a handler envelope into an MCP tools/call result.

    Text is the envelope minus `_mcp_*` keys. Image/audio blocks in
    `_mcp_content` are appended as sibling content entries. A
    `_mcp_media_path` is appended as a `MEDIA:` line so OpenClaw's
    Telegram delivery can `sendPhoto` without the model fetching URLs.
    """
    extra = result.get(MCP_CONTENT) or []
    media_path = result.get(MCP_MEDIA_PATH)
    text_body = {k: v for k, v in result.items() if not str(k).startswith("_mcp_")}
    text = json.dumps(text_body, default=str)
    if media_path:
        text = f"{text}\nMEDIA: {media_path}"
    return {
        "content": [{"type": "text", "text": text}, *extra],
        "isError": not result.get("ok", False)
                    and "requires_confirmation" not in result,
    }


# ---------------------------------------------------------------------------
# Secrets at rest
# ---------------------------------------------------------------------------

def fernet_key(raw: str) -> str:
    """Normalise a Fernet key read from the environment.

    A Fernet key is url-safe base64 of 32 bytes, so it always ends in exactly
    one '='. Shell .env parsers that split key=value on *every* '=' rather
    than the first one eat that padding (restore.sh did, for every restore),
    leaving a 43-character key that `Fernet()` rejects outright. Re-padding on
    read means a box whose .env was truncated by an older release still
    decrypts, instead of failing three layers away as an unexplained 401.

    Idempotent: a correctly padded key is returned unchanged.
    """
    raw = raw.strip()
    return raw + "=" * (-len(raw) % 4) if raw else ""


_decrypt_warned = False


def decrypt_secret(blob: str, key: str) -> str:
    """Decrypt a secret stored at rest. Returns "" when it cannot.

    Never returns `blob` on failure. Handing back an undecrypted Fernet token
    means the caller sends `gAAAAA...` over the wire as a bearer token and the
    user sees a bare 401 with nothing anywhere pointing at the key — the exact
    failure this function exists to make visible.

    Blobs that are not Fernet-shaped are passed through: accounts written
    before at-rest encryption existed hold plaintext, and those keep working.
    """
    if not blob:
        return ""
    if not key:
        return blob  # development / unencrypted mode
    try:
        from cryptography.fernet import Fernet  # type: ignore
        return Fernet(fernet_key(key).encode()).decrypt(blob.encode()).decode()
    except Exception:
        pass
    if not blob.startswith("gAAAAA"):
        return blob  # legacy plaintext, stored before at-rest encryption
    global _decrypt_warned
    if not _decrypt_warned:
        _decrypt_warned = True
        print(
            f"[homebrain] a stored secret could not be decrypted — wrong key or "
            f"corrupt value (key length {len(fernet_key(key))}, expected 44). "
            f"Treating the credential as missing rather than sending ciphertext.",
            file=sys.stderr, flush=True,
        )
    return ""


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

AUDIT_DIR = os.environ.get("HOMEBRAIN_AUDIT_DIR", "/var/log/homebrain")

def audit(server: str, action: str, **fields: Any) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "action": action,
        **fields,
    }
    line = json.dumps(rec, default=str)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR, f"mcp-{server}-audit.log")
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Consent token store — single-use, TTL-bounded, scoped to a chat-id.
# Stored at $HOME/.openclaw/pending_actions.json (mode 0600).
# ---------------------------------------------------------------------------

class Consent:
    PATH = os.environ.get(
        "HOMEBRAIN_PENDING_ACTIONS",
        os.path.expanduser("~/.openclaw/pending_actions.json"),
    )
    DEFAULT_TTL = 60  # seconds

    @classmethod
    def _load(cls) -> dict:
        try:
            with open(cls.PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _save(cls, store: dict) -> None:
        os.makedirs(os.path.dirname(cls.PATH), exist_ok=True)
        tmp = cls.PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(store).encode())
        finally:
            os.close(fd)
        os.replace(tmp, cls.PATH)

    @classmethod
    def _gc(cls, store: dict) -> dict:
        now = time.time()
        return {k: v for k, v in store.items() if v.get("expires_at", 0) > now}

    @classmethod
    def issue(cls, server: str, summary: str, payload: dict,
              chat_id: str | None = None,
              ttl: int = DEFAULT_TTL) -> str:
        """Create a single-use confirmation token and persist it.
        Returns the action_id the agent should echo back."""
        action_id = uuid.uuid4().hex
        store = cls._gc(cls._load())
        store[action_id] = {
            "server": server,
            "summary": summary,
            "payload": payload,
            "chat_id": chat_id,
            "issued_at": time.time(),
            "expires_at": time.time() + ttl,
        }
        cls._save(store)
        return action_id

    @classmethod
    def verify(cls, action_id: str, server: str,
               chat_id: str | None = None) -> dict | None:
        """Single-use redeem. Returns the original payload or None."""
        if not action_id:
            return None
        store = cls._gc(cls._load())
        rec = store.get(action_id)
        if not rec:
            cls._save(store)
            return None
        if rec.get("server") != server:
            return None
        if chat_id and rec.get("chat_id") and rec["chat_id"] != chat_id:
            return None
        # Single-use: delete on redeem.
        store.pop(action_id, None)
        cls._save(store)
        return rec.get("payload") or {}


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve(server_name: str,
          version: str,
          tools: list[dict],
          dispatch: Callable[[str, dict], dict]) -> None:
    """Tiny JSON-RPC 2.0 stdio loop, MCP 2025-06-18 spec.

    `tools` is the static tool catalogue (returned from tools/list).
    `dispatch(name, args) -> dict` is the user-supplied handler. The
    envelope it returns is wrapped by `tool_call_result` (text JSON plus
    any `_mcp_content` / `_mcp_media_path` the handler attached).
    """
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
            _write({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": server_name, "version": version},
                },
            })
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": msg_id,
                    "result": {"tools": tools}})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = dispatch(name, args)
                # Auto-confirm when consent is disabled: if the tool
                # returned a consent envelope but Consent.issue returned
                # None (disabled), re-dispatch with confirmation_token
                # so the tool executes directly.
                if (result.get("requires_confirmation")
                        and result.get("action_id")
                        and os.environ.get("HOMEBRAIN_MCP_CONSENT", "true").lower() == "false"):
                    args["confirmation_token"] = result["action_id"]
                    result = dispatch(name, args)
            except Exception as e:
                result = err(f"unhandled exception: {e}")
            _write({
                "jsonrpc": "2.0", "id": msg_id,
                "result": tool_call_result(result),
            })
        elif method == "notifications/initialized":
            pass
        elif msg_id is not None:
            _write({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601,
                          "message": f"method not found: {method}"},
            })
