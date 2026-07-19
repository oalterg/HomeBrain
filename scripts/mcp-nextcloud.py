#!/usr/bin/env python3
"""HomeBrain Nextcloud MCP server (multi-account).

Talks to one or more Nextcloud instances over WebDAV (files) and OCS
(notes, shares). Authenticates with an *app password* per account — never
the master NC password — created either automatically against the
HomeBrain-shipped NC, or pasted in from an external NC's
Personal → Security → App passwords flow.

Privacy posture (see INTEGRATIONS_PLAN.md §3.2):
  * `nc.files_list` returns paths and sizes only, never contents.
  * `nc.files_search` returns paths matching the query, never bodies.
  * `nc.files_download` is REVEAL tier — capped at 2 MB and audited.
  * Bigger files: agent gets a one-shot share link; the user opens it
    themselves so the LM never ingests the bytes.

Environment:
  NC_ACCOUNTS_FILE             path to ~/.openclaw/nc_accounts.json
                               (list of {name, base_url, user, token}
                               with token Fernet-encrypted using
                               HOMEBRAIN_INTEGRATIONS_KEY).
  HOMEBRAIN_INTEGRATIONS_KEY   Fernet key for at-rest decryption.

Legacy fallback (single-account installs pre-multi-account):
  NC_BASE_URL, NC_USER, NC_TOKEN, NC_TOKEN_FILE — used only if
  NC_ACCOUNTS_FILE is absent. The dashboard migrates these on first
  read.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

NC_ACCOUNTS_FILE = os.environ.get("NC_ACCOUNTS_FILE", "")
INTEGRATIONS_KEY = os.environ.get("HOMEBRAIN_INTEGRATIONS_KEY", "")

# Legacy single-account fallback — kept so this MCP keeps working if
# spawned before the dashboard migrates a legacy install.
LEGACY_BASE_URL = os.environ.get("NC_BASE_URL", "").rstrip("/")
LEGACY_USER = os.environ.get("NC_USER", "")
LEGACY_TOKEN_FILE = os.environ.get("NC_TOKEN_FILE", "")
LEGACY_TOKEN = os.environ.get("NC_TOKEN", "")

MAX_DOWNLOAD_BYTES = 2_000_000

DAV_NS = "{DAV:}"


def _decrypt(blob: str) -> str:
    if not INTEGRATIONS_KEY:
        return blob
    try:
        from cryptography.fernet import Fernet  # type: ignore
        return Fernet(INTEGRATIONS_KEY.encode()).decrypt(blob.encode()).decode()
    except Exception:
        return blob


def _accounts() -> list[dict]:
    if NC_ACCOUNTS_FILE and os.path.exists(NC_ACCOUNTS_FILE):
        try:
            with open(NC_ACCOUNTS_FILE) as f:
                data = json.load(f)
            return data.get("accounts", []) if isinstance(data, dict) else []
        except (OSError, json.JSONDecodeError):
            return []
    # Legacy single-account fallback.
    tok = ""
    if LEGACY_TOKEN_FILE and os.path.exists(LEGACY_TOKEN_FILE):
        try:
            tok = open(LEGACY_TOKEN_FILE).read().strip()
        except OSError:
            pass
    if not tok:
        tok = LEGACY_TOKEN.strip()
    if tok and LEGACY_BASE_URL and LEGACY_USER:
        return [{"name": "homebrain", "base_url": LEGACY_BASE_URL,
                 "user": LEGACY_USER, "token": tok}]
    return []


def _pick_account(name: str | None) -> dict | None:
    accounts = _accounts()
    if not accounts:
        return None
    if not name:
        return accounts[0] if len(accounts) == 1 else None
    for a in accounts:
        if a.get("name") == name:
            return a
    return None


def _account_or_err(args: dict) -> tuple[dict | None, dict | None]:
    name = (args.get("account") or "").strip() or None
    a = _pick_account(name)
    if a is not None:
        return a, None
    accounts = _accounts()
    if not accounts:
        return None, unavailable("no Nextcloud accounts configured")
    if not name and len(accounts) > 1:
        names = ", ".join(repr(x.get("name")) for x in accounts)
        return None, err(
            f"multiple NC accounts configured; pass `account` (one of: {names})",
            hint="Use nc.list_accounts to see the configured set.",
        )
    return None, err(f"account '{name}' not found",
                     hint="Use nc.list_accounts to see the configured set.")


def _auth_header(account: dict) -> str | None:
    user = account.get("user", "")
    tok = _decrypt(account.get("token") or "")
    if not user or not tok:
        return None
    raw = f"{user}:{tok}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _http(account: dict, method: str, path: str, body: bytes | None = None,
          headers: dict | None = None, timeout: int = 10,
          ocs: bool = False) -> tuple[int, bytes, dict]:
    auth = _auth_header(account)
    if not auth:
        return 0, b"account missing user or token", {}
    base = (account.get("base_url") or "").rstrip("/")
    url = f"{base}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", auth)
    if ocs:
        req.add_header("OCS-APIRequest", "true")
        req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except urllib.error.URLError as e:
        return 0, str(e).encode(), {}


def _dav_files_prefix(account: dict) -> str:
    return f"/remote.php/dav/files/{account.get('user', '')}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_list_accounts(_args: dict) -> dict:
    accounts = [{"name": a.get("name"), "base_url": a.get("base_url"),
                 "user": a.get("user")} for a in _accounts()]
    return ok(accounts=accounts, total=len(accounts),
              hint=("Pass `account: <name>` on other tools to pick one. "
                    "Single-account installs default to the only entry."))


def t_health(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body, _ = _http(account, "GET", "/status.php")
    if code != 200:
        return unavailable(f"Nextcloud '{account['name']}' at "
                           f"{account['base_url']} unreachable: {code}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse status.php")
    return ok(account=account["name"], version=data.get("versionstring"),
              installed=data.get("installed"),
              maintenance=data.get("maintenance"))


# --- WebDAV files ----------------------------------------------------------

PROPFIND_BODY = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:">'
    b'<d:prop><d:displayname/><d:getcontentlength/>'
    b'<d:getcontenttype/><d:resourcetype/><d:getlastmodified/></d:prop>'
    b'</d:propfind>'
)


def t_files_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    prefix = _dav_files_prefix(account)
    dav_path = f"{prefix}{path}"
    code, body, _ = _http(account, "PROPFIND", dav_path, PROPFIND_BODY,
                          headers={"Depth": "1",
                                   "Content-Type": "application/xml"})
    if code in (0, 401):
        return unavailable(f"Nextcloud unreachable or unauthorised ({code})")
    if code not in (207, 200):
        return err(f"PROPFIND failed: {code}",
                   body=body[:200].decode("utf-8", "replace"))
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return err("could not parse PROPFIND response")
    entries = []
    for resp in root.findall(f"{DAV_NS}response"):
        href = (resp.findtext(f"{DAV_NS}href") or "").rstrip("/")
        if not href or href.endswith(f"{prefix}{path.rstrip('/')}"):
            continue  # skip the directory itself
        propstat = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if propstat is None:
            continue
        is_dir = propstat.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
        size = propstat.findtext(f"{DAV_NS}getcontentlength") or ""
        modified = propstat.findtext(f"{DAV_NS}getlastmodified") or ""
        rel = href[len(prefix):] if href.startswith(prefix) else href
        entries.append({
            "path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "is_dir": is_dir,
            "size": int(size) if size.isdigit() else None,
            "modified": modified,
        })
    return ok(account=account["name"], entries=entries, total=len(entries))


def t_files_search(args: dict) -> dict:
    """Use Nextcloud's WebDAV SEARCH against the full file index."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    q = (args.get("query") or "").strip()
    if not q:
        return err("query is required")
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        f'  <d:basicsearch>'
        f'    <d:select><d:prop><oc:fileid/><d:displayname/>'
        f'      <d:getcontentlength/><d:resourcetype/></d:prop></d:select>'
        f'    <d:from><d:scope><d:href>/files/{account.get("user", "")}</d:href>'
        f'      <d:depth>infinity</d:depth></d:scope></d:from>'
        f'    <d:where><d:like><d:prop><d:displayname/></d:prop>'
        f'      <d:literal>%{q}%</d:literal></d:like></d:where>'
        f'  </d:basicsearch>'
        f'</d:searchrequest>'
    ).encode()
    code, resp, _ = _http(account, "SEARCH", "/remote.php/dav",
                          body, headers={"Content-Type": "application/xml"})
    if code not in (207, 200):
        return err(f"SEARCH failed: {code}",
                   body=resp[:200].decode("utf-8", "replace"))
    try:
        root = ET.fromstring(resp)
    except ET.ParseError:
        return err("could not parse search response")
    prefix = _dav_files_prefix(account)
    matches = []
    for r in root.findall(f"{DAV_NS}response"):
        href = (r.findtext(f"{DAV_NS}href") or "").rstrip("/")
        prop = r.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if prop is None:
            continue
        is_dir = prop.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
        size = prop.findtext(f"{DAV_NS}getcontentlength") or ""
        rel = href[len(prefix):] if href.startswith(prefix) else href
        matches.append({
            "path": rel,
            "is_dir": is_dir,
            "size": int(size) if size.isdigit() else None,
        })
        if len(matches) >= 100:
            break
    return ok(account=account["name"], results=matches, total=len(matches))


def t_files_download(args: dict) -> dict:
    """Fetch a small file's contents. Capped at 2 MB.
    Just call this directly; the runtime prompts the user for approval."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    summary = (f"Nextcloud ({account['name']}): download contents of {path} "
               f"(capped at {MAX_DOWNLOAD_BYTES // 1000} kB)")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "path": path},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    p = redeemed["path"] if redeemed.get("path") else path
    if not p.startswith("/"):
        p = "/" + p
    code, body, _ = _http(redeem_account, "GET",
                          f"{_dav_files_prefix(redeem_account)}{p}")
    if code in (0, 401):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 404:
        return err("file not found")
    if code != 200:
        return err(f"download failed: {code}")
    if len(body) > MAX_DOWNLOAD_BYTES:
        audit("nextcloud", "download.too_large",
              account=redeem_account["name"], path=p, bytes=len(body))
        return err(
            f"file is {len(body)} bytes (cap {MAX_DOWNLOAD_BYTES}); "
            "use nc.files_share for large files"
        )
    audit("nextcloud", "download", account=redeem_account["name"],
          path=p, bytes=len(body))
    try:
        text = body.decode("utf-8")
        return ok(account=redeem_account["name"], path=p,
                  encoding="utf-8", content=text, size=len(body))
    except UnicodeDecodeError:
        return ok(account=redeem_account["name"], path=p, encoding="base64",
                  content=base64.b64encode(body).decode(), size=len(body))


def t_files_share(args: dict) -> dict:
    """Create a public read-only share link. Just call this directly."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "").strip()
    expire_days = int(args.get("expire_days") or 7)
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    summary = (f"Nextcloud ({account['name']}): create public share link for "
               f"{path} (expires in {expire_days} days)")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "path": path,
                                   "expire_days": expire_days}, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account

    from urllib.parse import urlencode
    form = urlencode({
        "path": redeemed["path"],
        "shareType": "3",  # public link
        "permissions": "1",  # read-only
    }).encode()
    code, body, _ = _http(redeem_account, "POST",
                          "/ocs/v2.php/apps/files_sharing/api/v1/shares",
                          body=form,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          ocs=True)
    if code not in (200, 100):
        return err(f"share creation failed: {code}",
                   body=body[:200].decode("utf-8", "replace"))
    try:
        data = json.loads(body)
        url = ((data.get("ocs") or {}).get("data") or {}).get("url", "")
    except json.JSONDecodeError:
        url = ""
    audit("nextcloud", "share", account=redeem_account["name"],
          path=redeemed["path"], expire_days=expire_days)
    return ok(account=redeem_account["name"], share_url=url,
              expire_days=expire_days, path=redeemed["path"])


# --- Notes -----------------------------------------------------------------

def t_notes_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body, _ = _http(account, "GET", "/index.php/apps/notes/api/v1/notes",
                          headers={"Accept": "application/json"})
    if code != 200:
        return unavailable(f"Notes API returned {code}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse notes response")
    summaries = [{"id": n.get("id"), "title": n.get("title"),
                  "category": n.get("category"),
                  "modified": n.get("modified")} for n in data]
    return ok(account=account["name"], notes=summaries, total=len(summaries))


def t_notes_get(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    nid = args.get("id")
    if nid is None:
        return err("id is required")
    code, body, _ = _http(account, "GET",
                          f"/index.php/apps/notes/api/v1/notes/{int(nid)}",
                          headers={"Accept": "application/json"})
    if code == 404:
        return err("note not found")
    if code != 200:
        return unavailable(f"Notes API returned {code}")
    try:
        n = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse note")
    return ok(account=account["name"], id=n.get("id"), title=n.get("title"),
              content=n.get("content"), category=n.get("category"),
              modified=n.get("modified"))


def t_notes_create(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    title = args.get("title") or "(untitled)"
    content = args.get("content") or ""
    category = args.get("category") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = (f"Nextcloud ({account['name']}): create note '{title}' "
               f"({len(content)} chars)")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "title": title,
                                   "content": content, "category": category},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    body = json.dumps({"title": redeemed["title"], "content": redeemed["content"],
                       "category": redeemed["category"]}).encode()
    code, resp, _ = _http(redeem_account, "POST",
                          "/index.php/apps/notes/api/v1/notes",
                          body=body,
                          headers={"Content-Type": "application/json"})
    if code not in (200, 201):
        return err(f"note creation failed: {code}")
    try:
        n = json.loads(resp)
    except json.JSONDecodeError:
        n = {}
    audit("nextcloud", "notes.create", account=redeem_account["name"],
          title=redeemed["title"], note_id=n.get("id"))
    return ok(account=redeem_account["name"], id=n.get("id"), title=n.get("title"))


def t_notes_update(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    nid = args.get("id")
    if nid is None:
        return err("id is required")
    title = args.get("title") or ""
    content = args.get("content") or ""
    category = args.get("category")
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = (f"Nextcloud ({account['name']}): update note {nid}"
               f"{f' ({title})' if title else ''}")
    payload = {"account": account["name"], "id": int(nid),
               "title": title, "content": content}
    if category is not None:
        payload["category"] = category
    if not confirm:
        action_id = Consent.issue("nextcloud", summary, payload, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    body_dict: dict = {}
    if redeemed.get("title"):
        body_dict["title"] = redeemed["title"]
    if redeemed.get("content"):
        body_dict["content"] = redeemed["content"]
    if "category" in redeemed:
        body_dict["category"] = redeemed["category"]
    body = json.dumps(body_dict).encode()
    code, resp, _ = _http(redeem_account, "PUT",
                          f"/index.php/apps/notes/api/v1/notes/{int(redeemed['id'])}",
                          body=body,
                          headers={"Content-Type": "application/json"})
    if code != 200:
        return err(f"note update failed: {code}")
    try:
        n = json.loads(resp)
    except json.JSONDecodeError:
        n = {}
    audit("nextcloud", "notes.update", account=redeem_account["name"],
          note_id=redeemed["id"])
    return ok(account=redeem_account["name"], id=n.get("id"), title=n.get("title"))


_ACCOUNT_PROP = {
    "type": "string",
    "description": ("Configured account name to act on. Omit when only one "
                    "account is configured; required when multiple are. Use "
                    "nc.list_accounts to enumerate."),
}

TOOLS = [
    {"name": "nc.list_accounts",
     "description": "List configured Nextcloud accounts (name + base_url + user).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nc.health",
     "description": "Check Nextcloud reachability and report version.",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "nc.files_list",
     "description": ("List files in a Nextcloud folder. Returns paths, sizes, "
                     "and modification times only — never file contents."),
     "inputSchema": {"type": "object",
                     "properties": {
                         "path": {"type": "string",
                                  "description": "Folder path, e.g. '/Documents'. Default '/'."},
                         "account": _ACCOUNT_PROP,
                     }}},
    {"name": "nc.files_search",
     "description": ("Search Nextcloud files by name (substring match). "
                     "Returns paths only — never file contents."),
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["query"]}},
    {"name": "nc.files_download",
     "description": (
         "Fetch contents of a small file (≤2 MB). Just call this; the runtime "
         "will prompt the user for approval automatically. For larger files, "
         "use nc.files_share to get a link the user can open themselves."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.files_share",
     "description": ("Create a public read-only share link for a Nextcloud "
                     "file or folder. Just call this directly with the path "
                     "and expire_days; the runtime prompts the user for "
                     "approval automatically — you do not need to ask the "
                     "user for a token first."),
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "expire_days": {"type": "integer"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.notes_list",
     "description": "List Nextcloud Notes (titles and metadata only).",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "nc.notes_get",
     "description": "Fetch full content of one note by id.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "integer"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["id"]}},
    {"name": "nc.notes_create",
     "description": ("Create a Nextcloud note. Call this directly with "
                     "title/content; the runtime prompts the user for "
                     "approval automatically."),
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "content": {"type": "string"},
                                    "category": {"type": "string"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["title", "content"]}},
    {"name": "nc.notes_update",
     "description": ("Update an existing Nextcloud note by id. Only fields "
                     "you provide (title, content, category) are changed. "
                     "Consent-gated."),
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "integer"},
                                    "title": {"type": "string"},
                                    "content": {"type": "string"},
                                    "category": {"type": "string"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["id"]}},
]


DISPATCH = {
    "nc.list_accounts": t_list_accounts,
    "nc.health": t_health,
    "nc.files_list": t_files_list,
    "nc.files_search": t_files_search,
    "nc.files_download": t_files_download,
    "nc.files_share": t_files_share,
    "nc.notes_list": t_notes_list,
    "nc.notes_get": t_notes_get,
    "nc.notes_create": t_notes_create,
    "nc.notes_update": t_notes_update,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-nextcloud", "0.3.0", TOOLS, dispatch)
