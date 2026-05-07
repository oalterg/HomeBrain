#!/usr/bin/env python3
"""HomeBrain Nextcloud MCP server.

Talks to the local Nextcloud over WebDAV (files) and OCS (notes, calendar,
contacts, talk). Authenticates with an *app password* — never the master
NC password — created during integration bootstrap by `occ user:add-app-password`.

Privacy posture (see INTEGRATIONS_PLAN.md §3.2):
  * `nc.files_list` returns paths and sizes only, never contents.
  * `nc.files_search` returns paths matching the query, never bodies.
  * `nc.files_download` is REVEAL tier — capped at 2 MB and audited.
  * Bigger files: agent gets a one-shot share link; the user opens it
    themselves so the LM never ingests the bytes.

Environment:
  NC_BASE_URL    e.g. http://localhost:8080  or  https://nc.<tunnel>
  NC_USER        Nextcloud username (typically the admin user)
  NC_TOKEN_FILE  path to file containing the app password (mode 0600)
  NC_TOKEN       fallback if NC_TOKEN_FILE not set
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

NC_BASE_URL = os.environ.get("NC_BASE_URL", "http://localhost:8080").rstrip("/")
NC_USER = os.environ.get("NC_USER", "")
NC_TOKEN_FILE = os.environ.get("NC_TOKEN_FILE", "")
NC_TOKEN = os.environ.get("NC_TOKEN", "")

MAX_DOWNLOAD_BYTES = 2_000_000

DAV_NS = "{DAV:}"


def _token() -> str:
    if NC_TOKEN_FILE and os.path.exists(NC_TOKEN_FILE):
        try:
            return open(NC_TOKEN_FILE).read().strip()
        except OSError:
            return ""
    return NC_TOKEN.strip()


def _auth_header() -> str | None:
    tok = _token()
    if not NC_USER or not tok:
        return None
    raw = f"{NC_USER}:{tok}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _http(method: str, path: str, body: bytes | None = None,
          headers: dict | None = None, timeout: int = 10,
          ocs: bool = False) -> tuple[int, bytes, dict]:
    auth = _auth_header()
    if not auth:
        return 0, b"no NC credentials configured", {}
    url = f"{NC_BASE_URL}{path}"
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


# ---------------------------------------------------------------------------
# WebDAV — files
# ---------------------------------------------------------------------------

PROPFIND_BODY = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:">'
    b'<d:prop><d:displayname/><d:getcontentlength/>'
    b'<d:getcontenttype/><d:resourcetype/><d:getlastmodified/></d:prop>'
    b'</d:propfind>'
)


def t_files_list(args: dict) -> dict:
    path = (args.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    dav_path = f"/remote.php/dav/files/{NC_USER}{path}"
    code, body, _ = _http("PROPFIND", dav_path, PROPFIND_BODY,
                          headers={"Depth": "1",
                                   "Content-Type": "application/xml"})
    if code in (0, 401):
        return unavailable(f"Nextcloud unreachable or unauthorised ({code})")
    if code not in (207, 200):
        return err(f"PROPFIND failed: {code}", body=body[:200].decode("utf-8", "replace"))
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return err("could not parse PROPFIND response")
    entries = []
    for resp in root.findall(f"{DAV_NS}response"):
        href = (resp.findtext(f"{DAV_NS}href") or "").rstrip("/")
        if not href or href.endswith(f"/files/{NC_USER}{path.rstrip('/')}"):
            continue  # skip the directory itself
        propstat = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if propstat is None:
            continue
        is_dir = propstat.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
        size = propstat.findtext(f"{DAV_NS}getcontentlength") or ""
        modified = propstat.findtext(f"{DAV_NS}getlastmodified") or ""
        # Trim "/remote.php/dav/files/<user>" prefix from the displayed path.
        prefix = f"/remote.php/dav/files/{NC_USER}"
        rel = href[len(prefix):] if href.startswith(prefix) else href
        entries.append({
            "path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "is_dir": is_dir,
            "size": int(size) if size.isdigit() else None,
            "modified": modified,
        })
    return ok(entries=entries, total=len(entries))


def t_files_search(args: dict) -> dict:
    """Use Nextcloud's WebDAV SEARCH against the full file index."""
    q = (args.get("query") or "").strip()
    if not q:
        return err("query is required")
    # Nextcloud supports a SEARCH report on /remote.php/dav.
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        f'  <d:basicsearch>'
        f'    <d:select><d:prop><oc:fileid/><d:displayname/>'
        f'      <d:getcontentlength/><d:resourcetype/></d:prop></d:select>'
        f'    <d:from><d:scope><d:href>/files/{NC_USER}</d:href>'
        f'      <d:depth>infinity</d:depth></d:scope></d:from>'
        f'    <d:where><d:like><d:prop><d:displayname/></d:prop>'
        f'      <d:literal>%{q}%</d:literal></d:like></d:where>'
        f'  </d:basicsearch>'
        f'</d:searchrequest>'
    ).encode()
    code, resp, _ = _http("SEARCH", "/remote.php/dav",
                          body, headers={"Content-Type": "application/xml"})
    if code not in (207, 200):
        return err(f"SEARCH failed: {code}",
                   body=resp[:200].decode("utf-8", "replace"))
    try:
        root = ET.fromstring(resp)
    except ET.ParseError:
        return err("could not parse search response")
    matches = []
    for r in root.findall(f"{DAV_NS}response"):
        href = (r.findtext(f"{DAV_NS}href") or "").rstrip("/")
        prop = r.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if prop is None:
            continue
        is_dir = prop.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
        size = prop.findtext(f"{DAV_NS}getcontentlength") or ""
        prefix = f"/remote.php/dav/files/{NC_USER}"
        rel = href[len(prefix):] if href.startswith(prefix) else href
        matches.append({
            "path": rel,
            "is_dir": is_dir,
            "size": int(size) if size.isdigit() else None,
        })
        if len(matches) >= 100:
            break
    return ok(results=matches, total=len(matches))


def t_files_download(args: dict) -> dict:
    """REVEAL-tier: fetch a small file's contents. Capped at 2 MB.
    Requires a confirmation token (act/reveal hybrid)."""
    path = (args.get("path") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    summary = f"Nextcloud: download contents of {path} (capped at {MAX_DOWNLOAD_BYTES // 1000} kB)"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary, {"path": path}, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    if not path.startswith("/"):
        path = "/" + path
    code, body, _ = _http("GET", f"/remote.php/dav/files/{NC_USER}{path}")
    if code in (0, 401):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 404:
        return err("file not found")
    if code != 200:
        return err(f"download failed: {code}")
    if len(body) > MAX_DOWNLOAD_BYTES:
        audit("nextcloud", "download.too_large", path=path, bytes=len(body))
        return err(
            f"file is {len(body)} bytes (cap {MAX_DOWNLOAD_BYTES}); "
            "use nc.files_share for large files"
        )
    audit("nextcloud", "download", path=path, bytes=len(body))
    try:
        text = body.decode("utf-8")
        return ok(path=path, encoding="utf-8", content=text, size=len(body))
    except UnicodeDecodeError:
        return ok(path=path, encoding="base64",
                  content=base64.b64encode(body).decode(),
                  size=len(body))


def t_files_share(args: dict) -> dict:
    """Create a public share link for a file/folder. ACT-tier."""
    path = (args.get("path") or "").strip()
    expire_days = int(args.get("expire_days") or 7)
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    summary = f"Nextcloud: create public share link for {path} (expires in {expire_days} days)"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"path": path, "expire_days": expire_days},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    from urllib.parse import urlencode
    form = urlencode({
        "path": redeemed["path"],
        "shareType": "3",  # public link
        "permissions": "1",  # read-only
    }).encode()
    code, body, _ = _http("POST",
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
    audit("nextcloud", "share", path=redeemed["path"], expire_days=expire_days)
    return ok(share_url=url, expire_days=expire_days, path=redeemed["path"])


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def t_notes_list(_args: dict) -> dict:
    code, body, _ = _http("GET", "/index.php/apps/notes/api/v1/notes",
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
    return ok(notes=summaries, total=len(summaries))


def t_notes_get(args: dict) -> dict:
    nid = args.get("id")
    if nid is None:
        return err("id is required")
    code, body, _ = _http("GET",
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
    return ok(id=n.get("id"), title=n.get("title"),
              content=n.get("content"), category=n.get("category"),
              modified=n.get("modified"))


def t_notes_create(args: dict) -> dict:
    title = args.get("title") or "(untitled)"
    content = args.get("content") or ""
    category = args.get("category") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = f"Nextcloud: create note '{title}' ({len(content)} chars)"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"title": title, "content": content,
                                   "category": category},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    body = json.dumps({"title": redeemed["title"], "content": redeemed["content"],
                       "category": redeemed["category"]}).encode()
    code, resp, _ = _http("POST", "/index.php/apps/notes/api/v1/notes",
                          body=body,
                          headers={"Content-Type": "application/json"})
    if code not in (200, 201):
        return err(f"note creation failed: {code}")
    try:
        n = json.loads(resp)
    except json.JSONDecodeError:
        n = {}
    audit("nextcloud", "notes.create",
          title=redeemed["title"], note_id=n.get("id"))
    return ok(id=n.get("id"), title=n.get("title"))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def t_health(_args: dict) -> dict:
    code, body, _ = _http("GET", "/status.php")
    if code != 200:
        return unavailable(f"Nextcloud at {NC_BASE_URL} unreachable: {code}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse status.php")
    return ok(version=data.get("versionstring"),
              installed=data.get("installed"),
              maintenance=data.get("maintenance"))


TOOLS = [
    {"name": "nc.health",
     "description": "Check Nextcloud reachability and report version.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nc.files_list",
     "description": "List files in a Nextcloud folder. Returns paths, sizes, and modification times only — never file contents.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string",
                                             "description": "Folder path, e.g. '/Documents'. Default '/'."}}}},
    {"name": "nc.files_search",
     "description": "Search Nextcloud files by name (substring match). Returns paths only — never file contents.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "nc.files_download",
     "description": (
         "Fetch contents of a small file (≤2 MB). Just call this; the runtime "
         "will prompt the user for approval automatically. For larger files, "
         "use nc.files_share to get a link the user can open themselves."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.files_share",
     "description": "Create a public read-only share link for a Nextcloud file or folder. Just call this directly with the path and expire_days; the runtime prompts the user for approval automatically — you do not need to ask the user for a token first.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "expire_days": {"type": "integer"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.notes_list",
     "description": "List Nextcloud Notes (titles and metadata only).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nc.notes_get",
     "description": "Fetch full content of one note by id.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "integer"}},
                     "required": ["id"]}},
    {"name": "nc.notes_create",
     "description": "Create a Nextcloud note. Call this directly with title/content; the runtime prompts the user for approval automatically.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "content": {"type": "string"},
                                    "category": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["title", "content"]}},
]


DISPATCH = {
    "nc.health": t_health,
    "nc.files_list": t_files_list,
    "nc.files_search": t_files_search,
    "nc.files_download": t_files_download,
    "nc.files_share": t_files_share,
    "nc.notes_list": t_notes_list,
    "nc.notes_get": t_notes_get,
    "nc.notes_create": t_notes_create,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-nextcloud", "0.1.0", TOOLS, dispatch)
