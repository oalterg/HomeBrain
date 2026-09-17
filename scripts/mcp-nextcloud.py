#!/usr/bin/env python3
"""HomeBrain Nextcloud MCP server (multi-account).

Talks to one or more Nextcloud instances over WebDAV (files) and OCS
(notes, shares). Authenticates with an *app password* per account — never
the master NC password — created either automatically against the
HomeBrain-shipped NC, or pasted in from an external NC's
Personal → Security → App passwords flow.

Privacy posture (see INTEGRATIONS_PLAN.md §3.2):
  * `nc.files_list` / `nc.files_search` return paths and sizes only,
    never contents. Search is filename LIKE, not full text. Lists cap.
  * `nc.files_download` is REVEAL tier — capped at 20 MB and audited.
    It writes the file onto THIS HomeBrain under the OpenClaw workspace
    and returns a `media` path for the message tool. The envelope never
    carries base64 or file bytes (except small UTF-8 text ≤ TEXT_INGEST_MAX).
  * `nc.files_upload` / `mkdir` / `move` / `delete` are ACT tier.
    Upload reads a file already on THIS box (Telegram inbound or workspace)
    and PUTs it to WebDAV. The envelope never carries base64 or file bytes.
  * Shares: `share_with` is a Nextcloud username (works with no public URL).
    A public link on a LAN-only box is not reachable from Telegram — send
    the file with the message tool instead. expireDate is actually set.

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
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlencode, urlparse

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
LIST_MAX = 100
NOTES_LIST_MAX = 100
SHAREES_MAX = 10
EXPIRE_DAYS_DEFAULT = 7
EXPIRE_DAYS_MAX = 90
MOVE_TIMEOUT = 60

DAV_NS = "{DAV:}"
_LAN_SHARE_HINT = (
    "This link is only reachable on the home LAN. Telegram cannot open it. "
    "Pass share_with (nc.sharees) or nc.files_download and send in chat."
)
_USER_SHARE_HINT = (
    "They will see this in their Nextcloud. It is not a Telegram link."
)


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


def _encrypted_path(norm: str) -> bool:
    lower = (norm or "").lower()
    return (lower == "/documents (encrypted)"
            or lower.startswith("/documents (encrypted)/"))


def _dest_forbidden(norm: str) -> str | None:
    if _encrypted_path(norm):
        return ("dest is an end-to-end encrypted folder; "
                "the app-password user cannot write there")
    lower = (norm or "").lower()
    if lower == "/instantupload" or lower.startswith("/instantupload/"):
        return ("dest is the phone auto-upload folder; "
                "use /Photos/From chat/ or /Documents/From chat/")
    return None


def _xml_text(value: str) -> str:
    return (value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _like_contains_literal(query: str) -> str | None:
    """Strip LIKE wildcards, then XML-escape. None if nothing searchable."""
    q = (query or "").replace("\\", "").replace("%", "").replace("_", "").strip()
    if not q:
        return None
    return _xml_text(q)


def _expire_days(raw) -> tuple[int | None, dict | None]:
    if raw is None or raw == "":
        days = EXPIRE_DAYS_DEFAULT
    else:
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return None, err("expire_days must be an integer")
    if days < 1 or days > EXPIRE_DAYS_MAX:
        return None, err(f"expire_days must be 1–{EXPIRE_DAYS_MAX}")
    return days, None


def _expire_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=int(days))).date().isoformat()


def link_scope(url: str) -> str:
    """'lan' if Telegram cannot fetch this host; 'internet' otherwise."""
    host = (urlparse(url or "").hostname or "").lower().strip("[]")
    if not host:
        return "unknown"
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return "lan"
    if host.endswith(".local") or host.endswith(".localhost") or host.endswith(".lan"):
        return "lan"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "internet"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return "lan"
    return "internet"


def _ocs_data(code: int, body: bytes) -> tuple[object | None, dict | None]:
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {}
    if code in (401, 0):
        return None, unavailable("Nextcloud unreachable or unauthorised")
    ocs = parsed.get("ocs") if isinstance(parsed, dict) else None
    meta = (ocs or {}).get("meta") or {}
    statuscode = meta.get("statuscode")
    ok_http = code in (200, 201)
    ok_ocs = statuscode in (None, 100, 200, 201) or meta.get("status") == "ok"
    if not ok_http or not ok_ocs:
        msg = meta.get("message") or f"OCS failed: {code}"
        return None, err(msg)
    return (ocs or {}).get("data"), None


def _share_row(item: dict) -> dict:
    url = item.get("url") or ""
    row = {
        "id": item.get("id"),
        "share_type": item.get("share_type"),
        "share_with": item.get("share_with") or "",
        "path": item.get("path") or "",
        "url": url,
        "expiration": item.get("expiration") or "",
        "permissions": item.get("permissions"),
    }
    if url and link_scope(url) == "lan":
        row["link_scope"] = "lan"
        row["hint"] = _LAN_SHARE_HINT
    elif url:
        row["link_scope"] = "internet"
    return row


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
    path = _normalize_nc_path(args.get("path") or "/")
    if not path:
        return err("path is invalid")
    dav_path = _dav_path(account, path)
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
    prefix = _dav_files_prefix(account)
    self_norm = path.rstrip("/") or "/"
    entries = []
    truncated = False
    for resp in root.findall(f"{DAV_NS}response"):
        href = unquote((resp.findtext(f"{DAV_NS}href") or "").rstrip("/"))
        if not href:
            continue
        propstat = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if propstat is None:
            continue
        rel = href[len(prefix):] if href.startswith(prefix) else href
        if not rel.startswith("/"):
            rel = "/" + rel
        if _normalize_nc_path(rel) == self_norm:
            continue
        is_dir = propstat.find(f"{DAV_NS}resourcetype/{DAV_NS}collection") is not None
        size = propstat.findtext(f"{DAV_NS}getcontentlength") or ""
        modified = propstat.findtext(f"{DAV_NS}getlastmodified") or ""
        entries.append({
            "path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "is_dir": is_dir,
            "size": int(size) if size.isdigit() else None,
            "modified": modified,
        })
        if len(entries) >= LIST_MAX:
            truncated = True
            break
    return ok(account=account["name"], entries=entries, total=len(entries),
              truncated=truncated)


def t_files_search(args: dict) -> dict:
    """Use Nextcloud's WebDAV SEARCH against the file name index."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    q = (args.get("query") or "").strip()
    if not q:
        return err("query is required")
    literal = _like_contains_literal(q)
    if not literal:
        return err("query is required")
    user = _xml_text(account.get("user") or "")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        '  <d:basicsearch>'
        '    <d:select><d:prop><oc:fileid/><d:displayname/>'
        '      <d:getcontentlength/><d:resourcetype/></d:prop></d:select>'
        f'    <d:from><d:scope><d:href>/files/{user}</d:href>'
        '      <d:depth>infinity</d:depth></d:scope></d:from>'
        '    <d:where><d:like><d:prop><d:displayname/></d:prop>'
        f'      <d:literal>%{literal}%</d:literal></d:like></d:where>'
        '  </d:basicsearch>'
        '</d:searchrequest>'
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
    truncated = False
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
        if len(matches) >= LIST_MAX:
            truncated = True
            break
    return ok(account=account["name"], results=matches, total=len(matches),
              truncated=truncated)


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
    """Public link, or a user share when share_with is set. Consent-gated."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "").strip()
    share_with = (args.get("share_with") or "").strip()
    days, ebody = _expire_days(args.get("expire_days"))
    if ebody is not None:
        return ebody
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    norm = _normalize_nc_path(path)
    if not norm or norm == "/":
        return err("path is invalid")
    if _encrypted_path(norm):
        return err("path is an end-to-end encrypted folder")
    dav = _dav_path(account, norm)
    code, _, _ = _http(account, "HEAD", dav, timeout=15)
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
    kind = f"user {share_with}" if share_with else "public link"
    summary = (f"Nextcloud ({account['name']}): share {norm} with {kind} "
               f"(expires {_expire_iso(days)})")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "path": norm,
                                   "share_with": share_with,
                                   "expire_days": days}, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    dest = _normalize_nc_path(redeemed.get("path") or "")
    if not dest or dest == "/":
        return err("path is invalid")
    if _encrypted_path(dest):
        return err("path is an end-to-end encrypted folder")
    with_user = (redeemed.get("share_with") or "").strip()
    days = int(redeemed.get("expire_days") or days)
    expire = _expire_iso(days)
    form = {
        "path": dest,
        "shareType": "0" if with_user else "3",
        "permissions": "1",
        "expireDate": expire,
    }
    if with_user:
        form["shareWith"] = with_user
    code, body, _ = _http(
        redeem_account, "POST",
        "/ocs/v2.php/apps/files_sharing/api/v1/shares",
        body=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        ocs=True)
    data, ebody = _ocs_data(code, body)
    if ebody is not None:
        return ebody
    item = data if isinstance(data, dict) else {}
    audit("nextcloud", "share", account=redeem_account["name"],
          path=dest, expire_days=days, share_with=with_user or None)
    row = _share_row(item)
    row["account"] = redeem_account["name"]
    row["expire_days"] = days
    if row.get("url"):
        row["share_url"] = row["url"]
    if with_user and not row.get("hint"):
        row["hint"] = _USER_SHARE_HINT
    elif not with_user and not row.get("url"):
        row["hint"] = _LAN_SHARE_HINT
    return ok(**row)


# --- Notes -----------------------------------------------------------------

def t_notes_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    query = {"exclude": "content"}
    category = (args.get("category") or "").strip()
    if category:
        query["category"] = category
    path = "/index.php/apps/notes/api/v1/notes?" + urlencode(query)
    code, body, _ = _http(account, "GET", path,
                          headers={"Accept": "application/json"})
    if code != 200:
        return unavailable(f"Notes API returned {code}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse notes response")
    if not isinstance(data, list):
        data = []
    truncated = len(data) > NOTES_LIST_MAX
    summaries = [{"id": n.get("id"), "title": n.get("title"),
                  "category": n.get("category"),
                  "modified": n.get("modified")}
                 for n in data[:NOTES_LIST_MAX]]
    return ok(account=account["name"], notes=summaries, total=len(summaries),
              truncated=truncated)


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
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    payload: dict = {"account": account["name"], "id": int(nid)}
    if "title" in args:
        payload["title"] = args.get("title") or ""
    if "content" in args:
        payload["content"] = "" if args.get("content") is None else str(args.get("content"))
    if "category" in args:
        payload["category"] = args.get("category") or ""
    if not any(k in payload for k in ("title", "content", "category")):
        return err("nothing to update")
    title = payload.get("title") or ""
    summary = (f"Nextcloud ({account['name']}): update note {nid}"
               f"{f' ({title})' if title else ''}")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary, payload, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    body_dict: dict = {}
    if "title" in redeemed:
        body_dict["title"] = redeemed["title"]
    if "content" in redeemed:
        body_dict["content"] = redeemed["content"]
    if "category" in redeemed:
        body_dict["category"] = redeemed["category"]
    if not body_dict:
        return err("nothing to update")
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


def t_files_mkdir(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    dest_raw = (args.get("dest") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not dest_raw:
        return err("dest is required")
    dest = _normalize_nc_path(dest_raw)
    if not dest or dest == "/":
        return err("dest is invalid")
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)
    summary = f"Nextcloud ({account['name']}): create folder {dest}"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "dest": dest},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    dest = _normalize_nc_path(redeemed.get("dest") or dest)
    if not dest or dest == "/":
        return err("dest is invalid")
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)
    mk = _ensure_dav_parents(redeem_account, dest)
    if mk is not None:
        return mk
    code, body, _ = _http(redeem_account, "MKCOL", _dav_path(redeem_account, dest))
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 405:
        return err("folder already exists")
    if code == 403:
        return err(
            "access denied",
            hint="This path may be end-to-end encrypted or not shared "
                 "with the app-password user.",
        )
    if code not in (201, 200, 301):
        return err(f"mkdir failed: {code}",
                   body=(body[:200].decode("utf-8", "replace") if body else ""))
    audit("nextcloud", "mkdir", account=redeem_account["name"], path=dest)
    return ok(account=redeem_account["name"], nc_path=dest)


def t_files_move(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    src_raw = (args.get("src") or "").strip()
    dest_raw = (args.get("dest") or "").strip()
    overwrite = _as_bool(args.get("overwrite"), False)
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not src_raw:
        return err("src is required")
    if not dest_raw:
        return err("dest is required")
    src = _normalize_nc_path(src_raw)
    if not src or src == "/":
        return err("src is invalid")
    dest_is_folder = dest_raw.endswith("/")
    dest = _normalize_nc_path(dest_raw)
    if not dest:
        return err("dest is invalid")
    if dest_is_folder:
        dest = _normalize_nc_path(dest + "/" + src.rsplit("/", 1)[-1])
        if not dest:
            return err("dest is invalid")
    if dest == "/":
        return err("dest is invalid")
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)
    dav_src = _dav_path(account, src)
    code, _, _ = _http(account, "HEAD", dav_src, timeout=15)
    if code == 404:
        return err("src not found")
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err("access denied")
    summary = f"Nextcloud ({account['name']}): move {src} → {dest}"
    if overwrite:
        summary += " (overwrite)"
    if not confirm:
        action_id = Consent.issue(
            "nextcloud", summary,
            {"account": account["name"], "src": src, "dest": dest,
             "overwrite": overwrite},
            chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    src = _normalize_nc_path(redeemed.get("src") or src)
    dest = _normalize_nc_path(redeemed.get("dest") or dest)
    if not src or not dest or src == "/" or dest == "/":
        return err("path is invalid")
    blocked = _dest_forbidden(dest)
    if blocked:
        return err(blocked)
    mk = _ensure_dav_parents(redeem_account, dest)
    if mk is not None:
        return mk
    base = (redeem_account.get("base_url") or "").rstrip("/")
    dest_url = f"{base}{_dav_path(redeem_account, dest)}"
    ovw = "T" if redeemed.get("overwrite") else "F"
    code, body, _ = _http(
        redeem_account, "MOVE", _dav_path(redeem_account, src),
        headers={"Destination": dest_url, "Overwrite": ovw},
        timeout=MOVE_TIMEOUT)
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 404:
        return err("src not found")
    if code == 412:
        return err("dest already exists", hint="Pass overwrite=true to replace it.")
    if code == 403:
        return err("access denied")
    if code not in (201, 204, 200):
        return err(f"move failed: {code}",
                   body=(body[:200].decode("utf-8", "replace") if body else ""))
    audit("nextcloud", "move", account=redeem_account["name"],
          src=src, dest=dest)
    return ok(account=redeem_account["name"], src=src, nc_path=dest)


def t_files_delete(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    path = (args.get("path") or "").strip()
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not path:
        return err("path is required")
    norm = _normalize_nc_path(path)
    if not norm or norm == "/":
        return err("path is invalid")
    if _encrypted_path(norm):
        return err("path is an end-to-end encrypted folder")
    dav = _dav_path(account, norm)
    code, _, _ = _http(account, "HEAD", dav, timeout=15)
    if code == 404:
        return err("file not found")
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 403:
        return err("access denied")
    summary = (f"Nextcloud ({account['name']}): delete {norm} "
               "(folders are recursive)")
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "path": norm},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    dest = _normalize_nc_path(redeemed.get("path") or norm)
    if not dest or dest == "/":
        return err("path is invalid")
    if _encrypted_path(dest):
        return err("path is an end-to-end encrypted folder")
    code, body, _ = _http(redeem_account, "DELETE",
                          _dav_path(redeem_account, dest), timeout=60)
    if code in (401, 0):
        return unavailable("Nextcloud unreachable or unauthorised")
    if code == 404:
        return err("file not found")
    if code == 403:
        return err("access denied")
    if code not in (200, 204):
        return err(f"delete failed: {code}",
                   body=(body[:200].decode("utf-8", "replace") if body else ""))
    audit("nextcloud", "delete", account=redeem_account["name"], path=dest)
    return ok(account=redeem_account["name"], deleted=dest)


def t_shares_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    query = {}
    path = (args.get("path") or "").strip()
    if path:
        norm = _normalize_nc_path(path)
        if not norm:
            return err("path is invalid")
        query["path"] = norm
    qs = ("?" + urlencode(query)) if query else ""
    code, body, _ = _http(
        account, "GET",
        "/ocs/v2.php/apps/files_sharing/api/v1/shares" + qs,
        ocs=True)
    data, ebody = _ocs_data(code, body)
    if ebody is not None:
        return ebody
    items = data if isinstance(data, list) else []
    truncated = len(items) > LIST_MAX
    shares = [_share_row(i) for i in items[:LIST_MAX] if isinstance(i, dict)]
    return ok(account=account["name"], shares=shares, total=len(shares),
              truncated=truncated)


def t_share_delete(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    sid = args.get("id")
    if sid is None or str(sid).strip() == "":
        return err("id is required")
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = f"Nextcloud ({account['name']}): revoke share {sid}"
    if not confirm:
        action_id = Consent.issue("nextcloud", summary,
                                  {"account": account["name"], "id": str(sid)},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "nextcloud", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account")) or account
    rid = quote(str(redeemed.get("id") or sid), safe="")
    code, body, _ = _http(
        redeem_account, "DELETE",
        f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{rid}",
        ocs=True)
    _, ebody = _ocs_data(code, body)
    if ebody is not None:
        return ebody
    audit("nextcloud", "share.delete", account=redeem_account["name"], id=rid)
    return ok(account=redeem_account["name"], deleted=rid)


def t_sharees(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    q = (args.get("query") or "").strip()
    if not q:
        return err("query is required")
    qs = urlencode({
        "search": q,
        "itemType": "file",
        "lookup": "false",
        "perPage": str(SHAREES_MAX),
    })
    code, body, _ = _http(
        account, "GET",
        "/ocs/v2.php/apps/files_sharing/api/v1/sharees?" + qs,
        ocs=True)
    data, ebody = _ocs_data(code, body)
    if ebody is not None:
        return ebody
    users = []
    seen: set[str] = set()
    if isinstance(data, dict):
        buckets = []
        exact = data.get("exact") or {}
        if isinstance(exact, dict):
            buckets.extend(exact.get("users") or [])
        buckets.extend(data.get("users") or [])
        for row in buckets:
            if not isinstance(row, dict):
                continue
            val = row.get("value") or {}
            uid = val.get("shareWith") or ""
            if not uid or uid in seen:
                continue
            seen.add(uid)
            users.append({"share_with": uid, "label": row.get("label") or uid})
            if len(users) >= SHAREES_MAX:
                break
    return ok(account=account["name"], users=users, total=len(users))


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
     "description": "List a folder (paths, sizes, mtimes). Not contents. Capped.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "path": {"type": "string",
                                  "description": "Folder path. Default '/'."},
                         "account": _ACCOUNT_PROP,
                     }}},
    {"name": "nc.files_search",
     "description": "Search by filename, not contents. Paths only. Capped.",
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
    {"name": "nc.files_mkdir",
     "description": "Create a folder. Missing parents are created.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "dest": {"type": "string",
                                  "description": "Folder path, e.g. /Documents/Taxes."},
                         "account": _ACCOUNT_PROP,
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["dest"]}},
    {"name": "nc.files_move",
     "description": "Move or rename a file or folder.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "src": {"type": "string"},
                         "dest": {"type": "string",
                                  "description": "New path, or a folder ending in /."},
                         "overwrite": {
                             "type": "boolean",
                             "description": "Replace existing dest. Default false.",
                         },
                         "account": _ACCOUNT_PROP,
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["src", "dest"]}},
    {"name": "nc.files_delete",
     "description": "Delete a file or folder (folders are recursive).",
     "inputSchema": {"type": "object",
                     "properties": {
                         "path": {"type": "string"},
                         "account": _ACCOUNT_PROP,
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["path"]}},
    {"name": "nc.files_share",
     "description": (
         "Share a path. share_with=NC user (no public URL). Else a public "
         "link — LAN-only boxes: Telegram cannot open it; send in chat."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "share_with": {
                                        "type": "string",
                                        "description": "NC username. Prefer on LAN-only boxes.",
                                    },
                                    "expire_days": {
                                        "type": "integer",
                                        "description": "Days until expiry (1–90, default 7).",
                                    },
                                    "account": _ACCOUNT_PROP,
                                    "confirmation_token": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "nc.shares_list",
     "description": "List shares this account created. Optional path filter.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "path": {"type": "string"},
                         "account": _ACCOUNT_PROP,
                     }}},
    {"name": "nc.share_delete",
     "description": "Revoke a share by id from nc.shares_list.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "id": {"type": "string",
                                "description": "Share id from nc.shares_list."},
                         "account": _ACCOUNT_PROP,
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["id"]}},
    {"name": "nc.sharees",
     "description": "Look up Nextcloud users to pass as share_with.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "query": {"type": "string"},
                         "account": _ACCOUNT_PROP,
                     },
                     "required": ["query"]}},
    {"name": "nc.notes_list",
     "description": "List Nextcloud Notes (titles only). Optional category. Capped.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "category": {"type": "string"},
                         "account": _ACCOUNT_PROP,
                     }}},
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
    "nc.files_mkdir": t_files_mkdir,
    "nc.files_move": t_files_move,
    "nc.files_delete": t_files_delete,
    "nc.files_share": t_files_share,
    "nc.shares_list": t_shares_list,
    "nc.share_delete": t_share_delete,
    "nc.sharees": t_sharees,
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
    serve("homebrain-nextcloud", "0.6.0", TOOLS, dispatch)
