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
  * `nc.files_download` is REVEAL tier — capped at 20 MB and audited.
    It writes the file onto THIS HomeBrain under the OpenClaw workspace
    and returns a `media` path for the message tool. The envelope never
    carries base64 or file bytes (except small UTF-8 text ≤ TEXT_INGEST_MAX).
  * `nc.files_upload` is ACT tier — capped at 20 MB, audited. Reads a
    file already on THIS box (Telegram inbound or workspace) and PUTs it
    to WebDAV. The envelope never carries base64 or file bytes.
  * Bigger files: use `nc.files_share`; the user opens the link themselves
    so the LM never ingests the bytes.

Environment:
  NC_ACCOUNTS_FILE             path to ~/.openclaw/nc_accounts.json
                               (list of {name, base_url, user, token}
                               with token Fernet-encrypted using
                               HOMEBRAIN_INTEGRATIONS_KEY).
  HOMEBRAIN_INTEGRATIONS_KEY   Fernet key for at-rest decryption.
  HOMEBRAIN_NC_MEDIA_DIR       where downloads land
                               (default ~/.openclaw/workspace/media/nextcloud).
  HOMEBRAIN_OPENCLAW_WORKSPACE OpenClaw workspace root
                               (default ~/.openclaw/workspace); used to
                               make the returned `media` path relative.
  HOMEBRAIN_OC_MEDIA_INBOUND   OpenClaw Telegram inbound store
                               (default ~/.openclaw/media/inbound).

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
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, decrypt_secret, err, ok, serve, unavailable,
)

NC_ACCOUNTS_FILE = os.environ.get("NC_ACCOUNTS_FILE", "")
INTEGRATIONS_KEY = os.environ.get("HOMEBRAIN_INTEGRATIONS_KEY", "")

# Legacy single-account fallback — kept so this MCP keeps working if
# spawned before the dashboard migrates a legacy install.
LEGACY_BASE_URL = os.environ.get("NC_BASE_URL", "").rstrip("/")
LEGACY_USER = os.environ.get("NC_USER", "")
LEGACY_TOKEN_FILE = os.environ.get("NC_TOKEN_FILE", "")
LEGACY_TOKEN = os.environ.get("NC_TOKEN", "")

MAX_DOWNLOAD_BYTES = 20_000_000
MAX_UPLOAD_BYTES = MAX_DOWNLOAD_BYTES
TEXT_INGEST_MAX = 20_000  # characters
DOWNLOAD_TIMEOUT = 60
UPLOAD_TIMEOUT = 120

DAV_NS = "{DAV:}"


def _decrypt(blob: str) -> str:
    return decrypt_secret(blob, INTEGRATIONS_KEY)


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


def _normalize_nc_path(path: str) -> str | None:
    if path is None:
        return None
    s = str(path).strip()
    if not s:
        return None
    if not s.startswith("/"):
        s = "/" + s
    parts = []
    for seg in s.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            return None
        parts.append(seg)
    return "/" + "/".join(parts)


def _dav_path(account, path) -> str:
    user = quote(account.get("user", "") or "", safe="")
    segs = [quote(seg, safe="") for seg in str(path).strip("/").split("/") if seg]
    rest = "/".join(segs)
    if rest:
        return f"/remote.php/dav/files/{user}/{rest}"
    return f"/remote.php/dav/files/{user}"


def _header(headers, name) -> str:
    if not headers:
        return ""
    want = name.lower()
    for k, v in headers.items():
        if str(k).lower() == want:
            return "" if v is None else str(v)
    return ""


_EXT_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_HEIC_BRANDS = {b"heic", b"heix", b"heif", b"mif1", b"msf1"}
_OFFICE_ZIP_EXT = {"docx", "xlsx", "odt"}

_UPLOAD_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif",
    "application/pdf",
    "text/plain", "text/markdown", "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _filename_ext(filename: str) -> str:
    base = (filename or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _sniff_mime(head: bytes, declared: str, filename: str) -> str:
    head = head or b""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12].lower() in _HEIC_BRANDS:
        return "image/heic"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    ext = _filename_ext(filename)
    if head.startswith(b"PK") and ext in _OFFICE_ZIP_EXT:
        return _EXT_MIME[ext]
    ctype = (declared or "").split(";", 1)[0].strip().lower()
    if ctype and ctype not in ("application/octet-stream", "httpd/unix-directory"):
        return ctype
    return _EXT_MIME.get(ext, "application/octet-stream")


def _is_text_mime(mime: str) -> bool:
    m = (mime or "").lower()
    return m.startswith("text/") or m in ("application/json", "application/xml")


def _media_dir() -> str:
    return os.environ.get(
        "HOMEBRAIN_NC_MEDIA_DIR",
        os.path.expanduser("~/.openclaw/workspace/media/nextcloud"),
    )


def _workspace_dir() -> str:
    return os.environ.get(
        "HOMEBRAIN_OPENCLAW_WORKSPACE",
        os.path.expanduser("~/.openclaw/workspace"),
    )


def _inbound_dir() -> str:
    return os.environ.get(
        "HOMEBRAIN_OC_MEDIA_INBOUND",
        os.path.expanduser("~/.openclaw/media/inbound"),
    )


def _agent_media_path(path: str) -> str:
    """Workspace-relative path when the file landed under the OpenClaw
    workspace, otherwise the absolute path. The message tool resolves
    either; relative is what the agent should pass as `media`."""
    ws = os.environ.get(
        "HOMEBRAIN_OPENCLAW_WORKSPACE",
        os.path.expanduser("~/.openclaw/workspace"),
    )
    try:
        rel = os.path.relpath(path, ws)
    except ValueError:
        return path
    if rel.startswith(".."):
        return path
    return rel


def _safe_basename(nc_path: str) -> str:
    seg = (nc_path or "").rstrip("/").rsplit("/", 1)[-1]
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in seg)
    cleaned = cleaned[:120]
    return cleaned or "file"


def _prune_media(dest_dir: str, ttl: int = 86400) -> None:
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return
    cutoff = time.time() - ttl
    for name in names:
        fp = os.path.join(dest_dir, name)
        try:
            if not os.path.isfile(fp):
                continue
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            continue


def _http_get_capped(account, dav_path, dest_path, max_bytes,
                     timeout=DOWNLOAD_TIMEOUT) -> tuple[int, int, dict]:
    auth = _auth_header(account)
    if not auth:
        return 0, 0, {}
    base = (account.get("base_url") or "").rstrip("/")
    url = f"{base}{dav_path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            headers = dict(r.headers)
            cl = _header(headers, "Content-Length").strip()
            if cl.isdigit() and int(cl) > max_bytes:
                return 200, int(cl), headers
            fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            written = 0
            try:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        os.close(fd)
                        fd = -1
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        return 200, written, headers
                    os.write(fd, chunk)
            finally:
                if fd >= 0:
                    os.close(fd)
            return r.status, written, headers
    except urllib.error.HTTPError as e:
        try:
            e.read(200)
        except OSError:
            pass
        return e.code, 0, dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError):
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return 0, 0, {}


def _is_under(path: str, root: str) -> bool:
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def _allowed_media_roots() -> list[str]:
    return [_workspace_dir(), _inbound_dir(), _media_dir()]


def _resolve_local_media(raw: str) -> tuple[str | None, dict | None]:
    """Resolve an agent-supplied path to a real file under an allowed root.

    Accepts a workspace-relative path, an absolute path, or
    `media://inbound/<filename>` from OpenClaw's Telegram inbound store.
    Symlinks that escape the allowlist are rejected via realpath.
    """
    s = (raw or "").strip()
    if not s or "\x00" in s:
        return None, err("local_path is required" if not s else "local_path is invalid")
    prefix = "media://inbound/"
    if s.startswith(prefix):
        rest = s[len(prefix):]
        if (not rest or rest.endswith("/") or "/" in rest.replace("\\", "/")
                or rest in (".", "..")):
            return None, err("local_path is invalid")
        candidate = os.path.join(_inbound_dir(), rest)
    elif os.path.isabs(s):
        candidate = s
    else:
        ws_cand = os.path.join(_workspace_dir(), s)
        parts = s.replace("\\", "/").split("/")
        in_cand = os.path.join(_inbound_dir(), parts[-1]) if "inbound" in parts else ""
        if os.path.isfile(ws_cand):
            candidate = ws_cand
        elif in_cand and os.path.isfile(in_cand):
            candidate = in_cand
        else:
            candidate = ws_cand
    real = os.path.realpath(candidate)
    if not any(_is_under(real, root) for root in _allowed_media_roots()):
        return None, err(
            "local_path is not under an allowed directory",
            hint="Pass the inbound MediaPath or a path under the OpenClaw workspace.",
        )
    if not os.path.isfile(real):
        return None, err("local file not found")
    return real, None


def _local_file_meta(path: str) -> tuple[int, str, dict | None]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0, "", err("local file not found")
    if size <= 0:
        return 0, "", err("local file is empty")
    if size > MAX_UPLOAD_BYTES:
        return size, "", err(
            f"file is {size} bytes (cap {MAX_UPLOAD_BYTES})"
        )
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return 0, "", err("could not read local file")
    mime = _sniff_mime(head, "", os.path.basename(path))
    if mime not in _UPLOAD_MIME:
        return size, mime, err(
            f"file type {mime} is not allowed",
            hint="Photos and common documents only.",
        )
    return size, mime, None


def _dest_forbidden(norm: str) -> str | None:
    lower = (norm or "").lower()
    if (lower == "/documents (encrypted)"
            or lower.startswith("/documents (encrypted)/")):
        return ("dest is an end-to-end encrypted folder; "
                "the app-password user cannot write there")
    if lower == "/instantupload" or lower.startswith("/instantupload/"):
        return ("dest is the phone auto-upload folder; "
                "use /Photos/From chat/ or /Documents/From chat/")
    return None


def _as_bool(v, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes")


def _http_put_file(account, dav_path, local_path, mime, size,
                   timeout=UPLOAD_TIMEOUT) -> tuple[int, bytes, dict]:
    """Stream a local file as WebDAV PUT. Does not buffer the body in RAM."""
    auth = _auth_header(account)
    if not auth:
        return 0, b"account missing user or token", {}
    base = (account.get("base_url") or "").rstrip("/")
    url = f"{base}{dav_path}"
    try:
        with open(local_path, "rb") as f:
            req = urllib.request.Request(url, data=f, method="PUT")
            req.add_header("Authorization", auth)
            req.add_header("Content-Type", mime or "application/octet-stream")
            req.add_header("Content-Length", str(size))
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read(200), dict(r.headers)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read(200)
                except OSError:
                    body = b""
                return e.code, body, dict(e.headers or {})
    except OSError:
        return 0, b"could not read local file", {}
    except (urllib.error.URLError, TimeoutError):
        return 0, b"", {}


def _ensure_dav_parents(account, dest_file: str) -> dict | None:
    """MKCOL each parent of dest_file. 405 = already exists. None = ok."""
    parts = [p for p in dest_file.strip("/").split("/") if p]
    if len(parts) <= 1:
        return None
    acc = ""
    for seg in parts[:-1]:
        acc += "/" + seg
        code, body, _ = _http(account, "MKCOL", _dav_path(account, acc))
        if code in (201, 200, 405, 301):
            continue
        if code in (401, 0):
            return unavailable("Nextcloud unreachable or unauthorised")
        if code == 403:
            return err(
                "access denied",
                hint="This path may be end-to-end encrypted or not shared "
                     "with the app-password user.",
            )
        return err(
            f"could not create folder {acc}: {code}",
            body=(body[:200].decode("utf-8", "replace") if body else ""),
        )
    return None


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
        href = unquote((resp.findtext(f"{DAV_NS}href") or "").rstrip("/"))
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
        href = unquote((r.findtext(f"{DAV_NS}href") or "").rstrip("/"))
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


_FOLDER_HINT = "Pick a file, or use nc.files_share for a folder link."
_SEND_FILE_HINT = (
    "File saved on THIS HomeBrain at `media`. "
    "Send it with the message tool (media=<that path>). "
    "Do not paste the contents. For larger files, use nc.files_share."
)


def t_files_download(args: dict) -> dict:
    """Fetch a file ≤20 MB onto THIS HomeBrain. Consent-gated.

    Writes the bytes under the OpenClaw workspace and returns a `media`
    path for the message tool. Never returns base64 or file bytes in the
    envelope (except small UTF-8 text ≤ TEXT_INGEST_MAX).
    """
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    norm = _normalize_nc_path(path)
    if not norm:
        return err("path is invalid")
    if norm == "/":
        return err("path is a folder", hint=_FOLDER_HINT)
    dav = _dav_path(account, norm)
    code, _, head_headers = _http(account, "HEAD", dav, timeout=15)
    if code == 404:
        return err("file not found")
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err(
            "access denied",
            hint="This path may be end-to-end encrypted or not shared "
                 "with the app-password user.",
        )
    ctype = _header(head_headers, "Content-Type")
    if "httpd/unix-directory" in ctype.lower():
        return err("path is a folder", hint=_FOLDER_HINT)
    head_size = None
    cl = _header(head_headers, "Content-Length").strip()
    if cl.isdigit():
        head_size = int(cl)
        if head_size > MAX_DOWNLOAD_BYTES:
            audit("nextcloud", "download.too_large",
                  account=account["name"], path=norm, bytes=head_size)
            return err(
                f"file is {head_size} bytes (cap {MAX_DOWNLOAD_BYTES}); "
                "use nc.files_share for large files"
            )
    summary = (f"Nextcloud ({account['name']}): fetch {norm} onto this box "
               f"so it can be sent in chat")
    if head_size is not None:
        summary += f" ({head_size} bytes)"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "path": norm},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    p = redeemed["path"] if redeemed.get("path") else norm
    p = _normalize_nc_path(p)
    if not p:
        return err("path is invalid")
    if p == "/":
        return err("path is a folder", hint=_FOLDER_HINT)
    dav = _dav_path(redeem_account, p)

    dest_dir = _media_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
        try:
            os.chmod(dest_dir, 0o700)
        except OSError:
            pass
    except OSError:
        return err("could not write file to the OpenClaw workspace")
    dest = os.path.join(dest_dir, f"{int(time.time())}_{_safe_basename(p)}")
    code, nbytes, headers = _http_get_capped(
        redeem_account, dav, dest, MAX_DOWNLOAD_BYTES)
    if code == 404:
        return err("file not found")
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err(
            "access denied",
            hint="This path may be end-to-end encrypted or not shared "
                 "with the app-password user.",
        )
    if code != 200:
        return err(f"download failed: {code}")
    if nbytes > MAX_DOWNLOAD_BYTES:
        audit("nextcloud", "download.too_large",
              account=redeem_account["name"], path=p, bytes=nbytes)
        return err(
            f"file is {nbytes} bytes (cap {MAX_DOWNLOAD_BYTES}); "
            "use nc.files_share for large files"
        )
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return err("could not write file to the OpenClaw workspace")
    try:
        with open(dest, "rb") as f:
            head = f.read(65536)
    except OSError:
        return err("could not write file to the OpenClaw workspace")
    filename = _safe_basename(p)
    mime = _sniff_mime(head, _header(headers, "Content-Type"), filename)
    try:
        _prune_media(dest_dir)
    except OSError:
        pass
    audit("nextcloud", "download", account=redeem_account["name"],
          path=p, bytes=nbytes)
    payload = {
        "account": redeem_account["name"],
        "path": dest,
        "nc_path": p,
        "media": _agent_media_path(dest),
        "filename": filename,
        "mime_type": mime,
        "size": nbytes,
        "hint": _SEND_FILE_HINT,
    }
    if nbytes <= TEXT_INGEST_MAX and _is_text_mime(mime):
        try:
            with open(dest, encoding="utf-8") as f:
                text = f.read()
            if len(text) <= TEXT_INGEST_MAX:
                payload["content"] = text
                payload["encoding"] = "utf-8"
        except (UnicodeDecodeError, OSError):
            pass
    return ok(**payload)


_UPLOAD_HINT = "Saved on Nextcloud at `nc_path`. Do not paste the contents."
_DEST_FOLDER_HINT = "dest must be a file path, or a folder ending with /."


def t_files_upload(args: dict) -> dict:
    """PUT a local inbound/workspace file onto Nextcloud. Consent-gated.

    Reads bytes from disk and streams them to WebDAV. Never accepts or
    returns base64. local_path must resolve under the workspace, the
    OpenClaw inbound store, or HOMEBRAIN_NC_MEDIA_DIR.
    """
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    local_raw = (args.get("local_path") or "").strip()
    dest_raw = (args.get("dest") or "").strip()
    overwrite = _as_bool(args.get("overwrite"), False)
    if not local_raw:
        return err("local_path is required")
    if not dest_raw:
        return err("dest is required")

    local, ebody = _resolve_local_media(local_raw)
    if ebody is not None:
        return ebody
    size, mime, ebody = _local_file_meta(local)
    if ebody is not None:
        return ebody

    dest_is_folder = dest_raw.endswith("/")
    dest = _normalize_nc_path(dest_raw)
    if not dest:
        return err("dest is invalid")
    if dest_is_folder:
        fname = os.path.basename(local)
        if not fname or fname in (".", ".."):
            return err("dest is invalid")
        dest = _normalize_nc_path(dest + "/" + fname)
        if not dest:
            return err("dest is invalid")
    if dest == "/":
        return err("dest is a folder", hint=_DEST_FOLDER_HINT)
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)

    dav = _dav_path(account, dest)
    code, _, head_headers = _http(account, "HEAD", dav, timeout=15)
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err(
            "access denied",
            hint="This path may be end-to-end encrypted or not shared "
                 "with the app-password user.",
        )
    ctype = _header(head_headers, "Content-Type")
    if code == 200 and "httpd/unix-directory" in ctype.lower():
        return err("dest is a folder", hint=_DEST_FOLDER_HINT)
    if code == 200 and not overwrite:
        return err(
            "dest already exists",
            hint="Pass overwrite=true to replace it.",
        )

    summary = (
        f"Nextcloud ({account['name']}): upload {os.path.basename(local)} "
        f"({size} bytes, {mime}) to {dest}"
    )
    if overwrite and code == 200:
        summary += " (overwrite)"
    if not confirm:
        action_id = Consent.issue(
            "nextcloud", summary,
            {"account": account["name"], "local_path": local,
             "dest": dest, "overwrite": overwrite, "size": size, "mime": mime},
            chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    local, ebody = _resolve_local_media(redeemed.get("local_path") or local)
    if ebody is not None:
        return ebody
    size, mime, ebody = _local_file_meta(local)
    if ebody is not None:
        return ebody
    dest = _normalize_nc_path(redeemed.get("dest") or dest)
    if not dest or dest == "/":
        return err("dest is invalid")
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)

    mk = _ensure_dav_parents(redeem_account, dest)
    if mk is not None:
        return mk
    code, body, _ = _http_put_file(
        redeem_account, _dav_path(redeem_account, dest), local, mime, size)
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err(
            "access denied",
            hint="This path may be end-to-end encrypted or not shared "
                 "with the app-password user.",
        )
    if code == 409:
        return err("parent folder missing or dest is a folder")
    if code not in (200, 201, 204):
        return err(f"upload failed: {code}",
                   body=body[:200].decode("utf-8", "replace") if body else "")
    audit("nextcloud", "upload", account=redeem_account["name"],
          path=dest, bytes=size, mime=mime)
    return ok(
        account=redeem_account["name"],
        nc_path=dest,
        filename=os.path.basename(dest),
        mime_type=mime,
        size=size,
        hint=_UPLOAD_HINT,
    )


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
    "description": "Nextcloud account name. Required if several are configured.",
}

TOOLS = [
    {"name": "nc.list_accounts",
     "description": "List Nextcloud accounts (name, url, user).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nc.health",
     "description": "Check Nextcloud reachability and report version.",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "nc.files_list",
     "description": "List a folder (paths, sizes, mtimes). Not contents.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "path": {"type": "string",
                                  "description": "Folder path. Default '/'."},
                         "account": _ACCOUNT_PROP,
                     }}},
    {"name": "nc.files_search",
     "description": "Search files by name. Paths only — not contents.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["query"]}},
    {"name": "nc.files_download",
     "description": (
         "Fetch a file ≤20 MB onto this box. Returns `media`; send with "
         "the message tool (media=path). Don't paste contents. "
         "Larger: nc.files_share."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.files_upload",
     "description": (
         "Upload a file already on this box to Nextcloud. local_path from "
         "MediaPath; dest e.g. /Photos/From chat/x.jpg. Don't paste bytes."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "local_path": {
                             "type": "string",
                             "description": "Inbound MediaPath or workspace path.",
                         },
                         "dest": {
                             "type": "string",
                             "description": "NC path, e.g. /Photos/From chat/x.jpg",
                         },
                         "overwrite": {
                             "type": "boolean",
                             "description": "Replace existing dest. Default false.",
                         },
                         "account": _ACCOUNT_PROP,
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["local_path", "dest"]}},
    {"name": "nc.files_share",
     "description": "Create a public read-only share link.",
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
     "description": "Create a note.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "content": {"type": "string"},
                                    "category": {"type": "string"},
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["title", "content"]}},
    {"name": "nc.notes_update",
     "description": "Patch a note by id. Only supplied fields change.",
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
    "nc.files_upload": t_files_upload,
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
    serve("homebrain-nextcloud", "0.5.0", TOOLS, dispatch)
