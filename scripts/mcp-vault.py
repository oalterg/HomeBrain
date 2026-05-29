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
  returns an `unavailable` envelope rather than prompting — letting
  the agent decide how to ask the user.
* Only metadata is returned by `vault.search` (name, username, URI, login
  notes). Full secrets require explicit `vault.reveal` with a per-item ID,
  which is consent-gated and audited.

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

Dependencies
------------
* Bitwarden CLI: `npm install -g @bitwarden/cli` (the `bw` binary)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

VAULT_URL = os.environ.get("VAULT_URL", "")
VAULT_BW_BIN = os.environ.get("VAULT_BW_BIN", "bw")
VAULT_SESSION_FILE = os.environ.get(
    "VAULT_SESSION_FILE",
    os.path.expanduser("~/.openclaw/vault.session"),
)


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
        env["BW_SERVE_URL"] = VAULT_URL
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


def _session_or_unavail() -> tuple[str | None, dict | None]:
    session = _read_session()
    if not session:
        return None, unavailable(
            "vault is locked — unlock it from the HomeBrain dashboard")
    rc, out, _ = _bw("status", session=session)
    if rc != 0:
        return None, unavailable("vault status check failed")
    try:
        if json.loads(out).get("status") != "unlocked":
            return None, unavailable(
                "vault is locked — unlock it from the HomeBrain dashboard")
    except json.JSONDecodeError:
        return None, unavailable("could not parse vault status")
    return session, None


def _sync(session: str) -> None:
    """Pull the latest vault state from the server into the local CLI cache.

    The `bw` CLI serves reads (`list`, `get`) from a local encrypted cache,
    not from the server. Items created or edited in the web vault only live
    server-side until a sync, so without this every read path would silently
    miss them. Best-effort: a transient sync failure falls back to the cache
    rather than failing the whole call.
    """
    _bw("sync", session=session)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_status(_args: dict) -> dict:
    session = _read_session()
    if not session:
        return ok(unlocked=False, url=VAULT_URL)
    rc, out, _ = _bw("status", session=session)
    unlocked = False
    if rc == 0:
        try:
            unlocked = json.loads(out).get("status") == "unlocked"
        except json.JSONDecodeError:
            pass
    return ok(unlocked=unlocked, url=VAULT_URL)


def t_search(args: dict) -> dict:
    session, ebody = _session_or_unavail()
    if ebody is not None:
        return ebody
    _sync(session)
    q = (args.get("query") or "").strip()
    if q:
        rc, out, bw_err = _bw("list", "items", "--search", q, session=session)
    else:
        # No query → list every item. Lets the agent answer "show me all my
        # logins" instead of being limited to keyword search.
        rc, out, bw_err = _bw("list", "items", session=session)
    if rc != 0:
        return err(bw_err.strip() or "search failed")
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return err("could not parse bw output")
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
    audit("vault", "search", query=q or "(all)", hits=len(results))
    return ok(results=results, total=len(results))


def t_reveal(args: dict) -> dict:
    session, ebody = _session_or_unavail()
    if ebody is not None:
        return ebody
    item_id = (args.get("item_id") or "").strip()
    reason = args.get("reason") or "(unspecified)"
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not item_id:
        return err("item_id is required")

    summary = f"Vault: reveal password for item {item_id} (reason: {reason})"
    payload = {"item_id": item_id, "reason": reason}

    if not confirm:
        action_id = Consent.issue("vault", summary, payload, chat_id)
        return consent_required(action_id, summary)

    redeemed = Consent.verify(confirm, "vault", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    rid = redeemed["item_id"]
    _sync(session)
    rc, out, bw_err = _bw("get", "item", rid, session=session)
    if rc != 0:
        return err(bw_err.strip() or "item not found")
    try:
        item = json.loads(out)
    except json.JSONDecodeError:
        return err("could not parse bw output")
    login = item.get("login") or {}
    audit("vault", "reveal", item_id=rid,
          item_name=item.get("name"), reason=redeemed["reason"])
    return ok(
        id=item.get("id"),
        name=item.get("name"),
        username=login.get("username"),
        password=login.get("password"),
        totp_seed_present=bool(login.get("totp")),
        notes=item.get("notes"),
    )


def t_list_folders(_args: dict) -> dict:
    session, ebody = _session_or_unavail()
    if ebody is not None:
        return ebody
    _sync(session)
    rc, out, bw_err = _bw("list", "folders", session=session)
    if rc != 0:
        return err(bw_err.strip() or "list_folders failed")
    try:
        folders = json.loads(out)
    except json.JSONDecodeError:
        return err("could not parse bw output")
    return ok(folders=[{"id": f.get("id"), "name": f.get("name")}
                       for f in folders])


def t_create_login(args: dict) -> dict:
    session, ebody = _session_or_unavail()
    if ebody is not None:
        return ebody
    item_name = args.get("name") or "(unnamed)"
    username = args.get("username") or ""
    password = args.get("password") or ""
    uri = args.get("uri") or ""
    folder_id = args.get("folder_id") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")

    summary = (f"Vault: save new login '{item_name}' (user: {username}, "
               f"uri: {uri or 'none'})")
    payload = {"name": item_name, "username": username,
               "password": password, "uri": uri, "folder_id": folder_id}

    if not confirm:
        action_id = Consent.issue("vault", summary, payload, chat_id)
        return consent_required(action_id, summary)

    redeemed = Consent.verify(confirm, "vault", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    rc, tmpl_out, _ = _bw("get", "template", "item", session=session)
    if rc != 0:
        return err("could not fetch item template")
    try:
        tmpl = json.loads(tmpl_out)
    except json.JSONDecodeError:
        return err("could not parse item template")
    tmpl["type"] = 1  # login
    tmpl["name"] = redeemed["name"]
    tmpl["folderId"] = redeemed.get("folder_id") or None
    tmpl["login"] = {
        "username": redeemed["username"],
        "password": redeemed["password"],
        "uris": ([{"uri": redeemed["uri"], "match": None}]
                 if redeemed.get("uri") else []),
    }
    encoded = subprocess.run(
        [VAULT_BW_BIN, "encode"],
        input=json.dumps(tmpl), capture_output=True, text=True, timeout=10,
    )
    if encoded.returncode != 0:
        return err("bw encode failed")
    rc, out, bw_err = _bw("create", "item", encoded.stdout.strip(),
                           session=session)
    if rc != 0:
        return err(bw_err.strip() or "create failed")
    try:
        created = json.loads(out)
    except json.JSONDecodeError:
        return err("could not parse bw output")
    audit("vault", "create_login", item_id=created.get("id"),
          item_name=redeemed["name"], username=redeemed["username"],
          uri=redeemed.get("uri"))
    return ok(id=created.get("id"), name=created.get("name"))


TOOLS = [
    {"name": "vault.status",
     "description": "Check whether the vault is currently unlocked.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "vault.search",
     "description": (
         "List or search HomeBrain Vault items. Omit `query` to list every "
         "item in the vault; pass `query` to filter by free-text. Returns "
         "metadata only (name, username, URI) — no secrets. Always reflects "
         "the latest server state, including items added via the web vault."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "Optional free-text filter. Omit to list all items."},
         },
     }},
    {"name": "vault.reveal",
     "description": (
         "Reveal the password for a single vault item by ID. Audited and "
         "consent-gated. Use sparingly and only when the user has explicitly "
         "asked for the secret."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "item_id": {"type": "string", "description": "Vault item UUID"},
             "reason": {"type": "string",
                        "description": "Why the agent needs this — written to the audit log"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["item_id", "reason"],
     }},
    {"name": "vault.list_folders",
     "description": "List vault folders (id + name).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "vault.create_login",
     "description": (
         "Create a new login entry in the vault. Consent-gated — the runtime "
         "prompts the user for approval automatically."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "name": {"type": "string"},
             "username": {"type": "string"},
             "password": {"type": "string"},
             "uri": {"type": "string"},
             "folder_id": {"type": "string"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["name", "username", "password"],
     }},
]


DISPATCH = {
    "vault.status": t_status,
    "vault.search": t_search,
    "vault.reveal": t_reveal,
    "vault.list_folders": t_list_folders,
    "vault.create_login": t_create_login,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-vault", "0.3.0", TOOLS, dispatch)
