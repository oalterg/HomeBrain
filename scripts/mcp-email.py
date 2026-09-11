#!/usr/bin/env python3
"""HomeBrain Email MCP server (IMAP + SMTP, multi-account).

Reads accounts from ~/.openclaw/email_accounts.json (mode 0600). Each entry
holds host/port/user/password for IMAP and SMTP. Passwords are encrypted at
rest with a Fernet key derived from MASTER_PASSWORD by the dashboard; this
server gets the decrypted passwords passed in via the env var
HOMEBRAIN_EMAIL_KEY (a base64 Fernet key).

Tier policy (see INTEGRATIONS_PLAN.md §3.4):
  * READ      : email.list_unread, email.search, email.list_accounts.
                Subjects, senders, dates only — never bodies.
  * REVEAL    : email.fetch — full body. email.attachment — one file
                onto this box as `media` for the message tool. Audited.
                Consent-gated.
  * ACT       : email.draft (creates a draft, never sends), email.archive,
                email.flag. Consent-gated. email.send_direct is OFF by
                default (gated behind a settings-level toggle the dashboard
                sets via env HOMEBRAIN_EMAIL_SEND_DIRECT=true).

If HOMEBRAIN_EMAIL_KEY is not set, accounts are assumed to be stored in
plaintext (development mode). The dashboard always sets it on the live box.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import smtplib
import sys
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, decrypt_secret, err, ok, serve, unavailable,
)

ACCOUNTS_FILE = os.environ.get(
    "HOMEBRAIN_EMAIL_ACCOUNTS",
    os.path.expanduser("~/.openclaw/email_accounts.json"),
)
KEY_B64 = os.environ.get("HOMEBRAIN_EMAIL_KEY", "")
SEND_DIRECT_ENABLED = os.environ.get("HOMEBRAIN_EMAIL_SEND_DIRECT", "false").lower() == "true"

MAX_ATTACHMENT_BYTES = 20_000_000
_SEND_FILE_HINT = (
    "File saved on THIS HomeBrain at `media`. "
    "Send it with the message tool (media=<that path>). "
    "Do not paste the contents."
)


def _decrypt(blob: str) -> str:
    return decrypt_secret(blob, KEY_B64)


def _accounts() -> list[dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("accounts", []) if isinstance(data, dict) else []


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
        return None, unavailable("no email accounts configured")
    if not name and len(accounts) > 1:
        names = ", ".join(repr(x.get("name")) for x in accounts)
        return None, err(
            f"multiple email accounts configured; pass `account` (one of: {names})",
            hint="Use email.list_accounts to see the configured set.",
        )
    return None, err(f"account '{name}' not found",
                     hint="Use email.list_accounts to see the configured set.")


def _imap(account: dict) -> imaplib.IMAP4 | None:
    host = account.get("imap_host", "")
    port = int(account.get("imap_port", 993))
    user = account.get("user", "")
    pw = _decrypt(account.get("imap_password", ""))
    try:
        if account.get("imap_starttls"):
            conn = imaplib.IMAP4(host, port)
            conn.starttls()
        else:
            conn = imaplib.IMAP4_SSL(host, port) if port == 993 else imaplib.IMAP4(host, port)
        conn.login(user, pw)
        return conn
    except Exception as e:
        audit("email", "imap_error", account=account.get("name"), error=str(e))
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_list_accounts(_args: dict) -> dict:
    accounts = []
    for a in _accounts():
        role = "agent_mailbox" if a.get("agent_mailbox") else "owner_inbox"
        accounts.append({
            "name": a.get("name"),
            "user": a.get("user"),
            "role": role,
        })
    return ok(accounts=accounts, total=len(accounts),
              send_direct_enabled=SEND_DIRECT_ENABLED)


_APP_PART_RE = re.compile(
    r'"application"\s+"[^"]+"\s+\(([^)]*)\)', re.IGNORECASE)
_ATTACH_DISP_RE = re.compile(
    r'\(\s*"attachment"\s+\(([^)]*)\)', re.IGNORECASE)
_NAME_PARAM_RE = re.compile(
    r'"(?:name|filename)"\s+"([^"]+)"', re.IGNORECASE)


def _hdr(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _uid_bytes(uid) -> bytes:
    if isinstance(uid, (bytes, bytearray)):
        return bytes(uid)
    return str(uid).encode()


def _uid_str(uid) -> str:
    if isinstance(uid, (bytes, bytearray)):
        return uid.decode()
    return str(uid)


def _fetch_literal(data) -> bytes | None:
    if not data or not data[0]:
        return None
    item = data[0]
    if isinstance(item, tuple) and len(item) >= 2:
        payload = item[1]
        return bytes(payload) if isinstance(payload, (bytes, bytearray)) else None
    return None


def _imap_payload(data) -> bytes:
    if not data or not data[0]:
        return b""
    item = data[0]
    if isinstance(item, tuple):
        return b"".join(x for x in item if isinstance(x, (bytes, bytearray)))
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    return b""


def _filenames_from_structure(struct: bytes | str) -> list[str]:
    """Filenames from application/* parts and disposition=attachment.

    Skips HTML-embedded images (image/* + inline). Duplicate name/filename
    params on the same part are collapsed.
    """
    text = struct.decode("utf-8", "replace") if isinstance(struct, (bytes, bytearray)) else str(struct)
    names: list[str] = []
    seen: set[str] = set()
    chunks = [m.group(1) for m in _APP_PART_RE.finditer(text)]
    chunks.extend(m.group(1) for m in _ATTACH_DISP_RE.finditer(text))
    for chunk in chunks:
        for name in _NAME_PARAM_RE.findall(chunk):
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    return names


def _structure_has_attachments(struct: bytes | str) -> bool:
    """True if IMAP BODYSTRUCTURE describes a real file (PDF, etc.).

    Apple Mail forwards invoices as application/pdf with disposition INLINE,
    so we cannot key only on ATTACHMENT. RFC822.HEADER is not enough: the
    MIME parts live in the body, and a headers-only parse always looks empty.
    """
    if _filenames_from_structure(struct):
        return True
    text = struct.decode("utf-8", "replace") if isinstance(struct, (bytes, bytearray)) else str(struct)
    s = text.lower()
    return '"application"' in s or '("attachment"' in s


def _summarise(msg_bytes: bytes, uid: str, has_attachments: bool | None = None,
               attachments: list[dict] | None = None) -> dict:
    msg = email.message_from_bytes(msg_bytes)
    received = ""
    if msg.get("Date"):
        try:
            received = parsedate_to_datetime(msg["Date"]).isoformat()
        except (TypeError, ValueError):
            pass
    if attachments is None:
        attachments = [{"filename": n} for _, n, _ in _attachment_catalog(msg)]
    if has_attachments is None:
        has_attachments = bool(attachments)
    return {
        "id": uid,
        "from": _hdr(msg.get("From", "")),
        "to": _hdr(msg.get("To", "")),
        "subject": _hdr(msg.get("Subject", "")),
        "received": received,
        "has_attachments": bool(has_attachments),
        "attachments": attachments,
    }


def _uid_search(conn, *criteria) -> list[bytes]:
    rc, ids = conn.uid("SEARCH", *criteria)
    if rc != "OK" or not ids or not ids[0]:
        return []
    return ids[0].split()


def _peek_message(conn, uid):
    rc, data = conn.uid("FETCH", _uid_bytes(uid), "(BODY.PEEK[])")
    raw = _fetch_literal(data) if rc == "OK" else None
    if not raw:
        return None
    return email.message_from_bytes(raw)


def _list_from_imap(conn, uids: list) -> list[dict]:
    out = []
    for uid in reversed(uids):
        rc, data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        header = _fetch_literal(data) if rc == "OK" else None
        if not header:
            continue
        rc_s, sdata = conn.uid("FETCH", uid, "(BODYSTRUCTURE)")
        struct = _imap_payload(sdata) if rc_s == "OK" else b""
        names = _filenames_from_structure(struct)
        out.append(_summarise(
            header, _uid_str(uid),
            has_attachments=_structure_has_attachments(struct),
            attachments=[{"filename": n} for n in names],
        ))
    return out


def t_list_unread(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    limit = int(args.get("limit") or 20)
    conn = _imap(acc)
    if not conn:
        return unavailable(f"could not connect to IMAP for '{name}'")
    try:
        conn.select("INBOX", readonly=True)
        uids = _uid_search(conn, "UNSEEN")
        if not uids:
            return ok(account=name, messages=[], total=0)
        uids = uids[-limit:]
        out = _list_from_imap(conn, uids)
        return ok(account=name, messages=out, total=len(out))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def t_search(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    query = args.get("query") or ""
    limit = int(args.get("limit") or 30)
    if not query:
        return err("query is required")
    conn = _imap(acc)
    if not conn:
        return unavailable(f"could not connect to IMAP for '{name}'")
    try:
        conn.select("INBOX", readonly=True)
        # IMAP TEXT search — matches headers + body; returns UIDs only.
        uids = _uid_search(conn, "TEXT", f'"{query}"')
        if not uids:
            return ok(account=name, messages=[], total=0)
        uids = uids[-limit:]
        out = _list_from_imap(conn, uids)
        return ok(account=name, messages=out, total=len(out))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _decode_part_text(part) -> str:
    try:
        return (part.get_payload(decode=True) or b"").decode(
            part.get_content_charset() or "utf-8", "replace")
    except Exception:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""


def _message_body(msg) -> str:
    """Plaintext if present, otherwise HTML. Skip file parts."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                if (part.get_content_disposition() or "").lower() == "attachment":
                    continue
                plain = _decode_part_text(part)
            elif ctype == "text/html" and not html:
                if (part.get_content_disposition() or "").lower() == "attachment":
                    continue
                html = _decode_part_text(part)
        return plain or html
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        payload = msg.get_payload()
        return payload if isinstance(payload, str) else ""


def t_fetch(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    msg_id = args.get("id") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not msg_id:
        return err("id is required")
    summary = f"Email: read full body of message {msg_id} from account '{name}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _pick_account(redeemed["account"])
    if not acc:
        return err("account not found")
    conn = _imap(acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX", readonly=True)
        msg = _peek_message(conn, redeemed["id"])
        if msg is None:
            return err("message not found")
        body = _message_body(msg)
        parts = _attachment_catalog(msg)
        audit("email", "fetch", account=redeemed["account"], id=redeemed["id"])
        return ok(
            id=redeemed["id"],
            from_=_hdr(msg.get("From", "")),
            to=_hdr(msg.get("To", "")),
            subject=_hdr(msg.get("Subject", "")),
            received=msg.get("Date", ""),
            body=body[:50_000],  # hard cap to keep token usage sane
            truncated=len(body) > 50_000,
            attachments=[{"filename": n, "size": len(b)} for _, n, b in parts],
        )
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _is_keepable(part) -> bool:
    """True for real file parts, including Apple Mail inline PDFs.

    HTML-embedded images (inline / CID) are skipped; application/* and
    other named non-body parts are kept even when disposition is inline.
    """
    if part.get_content_maintype() == "multipart":
        return False
    ctype = part.get_content_type()
    if ctype in ("text/plain", "text/html"):
        return False
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    if ctype.startswith("image/"):
        return False
    if part.get_content_maintype() == "application":
        return True
    return bool(part.get_filename())


def _attachment_catalog(msg) -> list[tuple[object, str, bytes]]:
    """Keepable parts as (part, filename, decoded bytes)."""
    out = []
    for part in msg.walk():
        if not _is_keepable(part):
            continue
        try:
            body = part.get_payload(decode=True) or b""
        except Exception:
            body = b""
        name = part.get_filename() or "attachment"
        out.append((part, name, body))
    return out


def _sniff_mime(head: bytes, declared: str, filename: str) -> str:
    head = head or b""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    ctype = (declared or "").split(";", 1)[0].strip().lower()
    if ctype and ctype not in ("application/octet-stream",):
        return ctype
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "pdf": "application/pdf", "gif": "image/gif", "webp": "image/webp",
        "txt": "text/plain", "csv": "text/csv",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(ext, "application/octet-stream")


def _media_dir() -> str:
    return os.environ.get(
        "HOMEBRAIN_EMAIL_MEDIA_DIR",
        os.path.expanduser("~/.openclaw/workspace/media/email"),
    )


def _agent_media_path(path: str) -> str:
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


def _safe_basename(name: str) -> str:
    seg = (name or "").rstrip("/").rsplit("/", 1)[-1]
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in seg)
    return (cleaned[:120] or "attachment")


def _prune_media(dest_dir: str, ttl: int = 86400) -> None:
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return
    cutoff = time.time() - ttl
    for name in names:
        fp = os.path.join(dest_dir, name)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            continue


def t_attachment(args: dict) -> dict:
    """Save one email attachment onto this box for the message tool."""
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    msg_id = (args.get("id") or "").strip()
    filename = (args.get("filename") or "").strip() or None
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not msg_id:
        return err("id is required")
    summary = f"Email: fetch attachment from message {msg_id} on '{name}'"
    if filename:
        summary += f" ({filename})"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id,
                                   "filename": filename},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _pick_account(redeemed["account"])
    if not acc:
        return err("account not found")
    want = redeemed.get("filename") or filename
    conn = _imap(acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX", readonly=True)
        msg = _peek_message(conn, redeemed["id"])
        if msg is None:
            return err("message not found")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    parts = _attachment_catalog(msg)
    catalog = [{"filename": n, "size": len(body)} for _, n, body in parts]
    if not parts:
        audit("email", "attachment.none", account=redeemed["account"],
              id=redeemed["id"])
        return err("no attachments")
    chosen = None
    if want:
        needle = want.lower()
        matches = [(p, n, b) for p, n, b in parts if needle in n.lower()]
        if not matches:
            return err("attachment not found", attachments=catalog)
        chosen = matches[0]
    elif len(parts) == 1:
        chosen = parts[0]
    else:
        return ok(id=redeemed["id"], attachments=catalog,
                  hint="Several attachments; call again with filename.")
    _part, fname, body = chosen
    if len(body) > MAX_ATTACHMENT_BYTES:
        audit("email", "attachment.too_large", account=redeemed["account"],
              id=redeemed["id"], filename=fname, bytes=len(body))
        return err(
            f"file is {len(body)} bytes (cap {MAX_ATTACHMENT_BYTES})",
            filename=fname,
        )
    if not body:
        return err("attachment is empty", filename=fname)
    dest_dir = _media_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
        try:
            os.chmod(dest_dir, 0o700)
        except OSError:
            pass
    except OSError:
        return err("could not write file to the OpenClaw workspace")
    dest = os.path.join(dest_dir, f"{int(time.time())}_{_safe_basename(fname)}")
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
    except OSError:
        return err("could not write file to the OpenClaw workspace")
    mime = _sniff_mime(body[:64], _part.get_content_type(), fname)
    try:
        _prune_media(dest_dir)
    except OSError:
        pass
    audit("email", "attachment", account=redeemed["account"],
          id=redeemed["id"], filename=fname, bytes=len(body))
    return ok(
        account=redeemed["account"],
        id=redeemed["id"],
        path=dest,
        media=_agent_media_path(dest),
        filename=_safe_basename(fname),
        mime_type=mime,
        size=len(body),
        hint=_SEND_FILE_HINT,
    )


def _save_draft(account: dict, to: str, subject: str, body: str) -> tuple[bool, str]:
    """Append a message to the IMAP Drafts folder. Returns (ok, info)."""
    msg = EmailMessage()
    msg["From"] = account.get("user", "")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    drafts_folder = account.get("drafts_folder") or "Drafts"
    conn = _imap(account)
    if not conn:
        return False, "could not connect to IMAP"
    try:
        rc, _ = conn.append(drafts_folder, r"\Draft", None, msg.as_bytes())
        return rc == "OK", drafts_folder
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def t_draft(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    to = args.get("to") or ""
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not all([to, subject]):
        return err("to and subject are required")
    summary = f"Email: create DRAFT to {to} via '{name}', subject '{subject}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "to": to,
                                   "subject": subject, "body": body},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _pick_account(redeemed["account"])
    if not acc:
        return err("account not found")
    saved, info = _save_draft(acc, redeemed["to"], redeemed["subject"], redeemed["body"])
    if not saved:
        return err(f"draft creation failed: {info}")
    audit("email", "draft", account=redeemed["account"],
          to=redeemed["to"], subject=redeemed["subject"])
    return ok(folder=info, account=redeemed["account"],
              to=redeemed["to"], subject=redeemed["subject"])


def t_send_direct(args: dict) -> dict:
    if not SEND_DIRECT_ENABLED:
        return err(
            "email.send_direct is disabled",
            hint="Enable in HomeBrain dashboard → Connections → Email → Settings",
        )
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    to = args.get("to") or ""
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not all([to, subject]):
        return err("to and subject are required")
    summary = f"Email: SEND to {to} via '{name}', subject '{subject}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "to": to,
                                   "subject": subject, "body": body},
                                  chat_id, ttl=120)  # extra time for sends
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _pick_account(redeemed["account"])
    if not acc:
        return err("account not found")

    msg = EmailMessage()
    msg["From"] = acc.get("user", "")
    msg["To"] = redeemed["to"]
    msg["Subject"] = redeemed["subject"]
    msg.set_content(redeemed["body"])

    smtp_host = acc.get("smtp_host", "")
    smtp_port = int(acc.get("smtp_port", 587))
    smtp_user = acc.get("user", "")
    smtp_pw = _decrypt(acc.get("smtp_password", "") or acc.get("imap_password", ""))
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as s:
                s.login(smtp_user, smtp_pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.starttls()
                s.login(smtp_user, smtp_pw)
                s.send_message(msg)
    except Exception as e:
        audit("email", "send.fail", account=redeemed["account"],
              to=redeemed["to"], error=str(e))
        return err(f"send failed: {e}")
    audit("email", "send", account=redeemed["account"],
          to=redeemed["to"], subject=redeemed["subject"])
    return ok(sent=True, account=redeemed["account"], to=redeemed["to"])


def t_archive(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    msg_id = args.get("id") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not msg_id:
        return err("id is required")
    summary = f"Email: archive message {msg_id} on account '{name}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_acc = _pick_account(redeemed["account"])
    if not redeem_acc:
        return err("account not found")
    conn = _imap(redeem_acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX")
        archive = redeem_acc.get("archive_folder") or "Archive"
        try:
            conn.create(archive)
        except Exception:
            pass
        uid = _uid_bytes(redeemed["id"])
        conn.uid("COPY", uid, archive)
        conn.uid("STORE", uid, "+FLAGS", r"(\Deleted \Seen)")
        conn.expunge()
        audit("email", "archive", account=redeemed["account"], id=redeemed["id"])
        return ok(archived=True, folder=archive)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def t_flag(args: dict) -> dict:
    acc, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    name = acc["name"]
    msg_id = args.get("id") or ""
    remove = bool(args.get("remove", False))
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not msg_id:
        return err("id is required")
    action = "unflag" if remove else "flag"
    summary = f"Email: {action} message {msg_id} on account '{name}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id,
                                   "remove": remove}, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_acc = _pick_account(redeemed["account"])
    if not redeem_acc:
        return err("account not found")
    conn = _imap(redeem_acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX")
        op = "-FLAGS" if redeemed.get("remove") else "+FLAGS"
        conn.uid("STORE", _uid_bytes(redeemed["id"]), op, r"(\Flagged)")
        audit("email", "flag", account=redeemed["account"],
              id=redeemed["id"], remove=redeemed.get("remove", False))
        return ok(flagged=not redeemed.get("remove"), id=redeemed["id"])
    finally:
        try:
            conn.logout()
        except Exception:
            pass


TOOLS = [
    {"name": "email.list_accounts",
     "description": (
         "Your mailboxes (role=agent_mailbox) and owner inboxes you may "
         "operate (role=owner_inbox). Names, addresses, role — never "
         "credentials."
     ),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "email.list_unread",
     "description": (
         "Unread headers by IMAP UID. Includes filenames; inline PDFs count. "
         "Does not mark seen."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "limit": {"type": "integer"}}}},
    {"name": "email.search",
     "description": (
         "IMAP TEXT search. IMAP UID + filenames. Does not mark seen."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "email.fetch",
     "description": (
         "Body by IMAP UID (plain, else HTML). Lists filenames. Does not "
         "mark seen."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "id": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["id"]}},
    {"name": "email.attachment",
     "description": (
         "Save one file by IMAP UID, including inline PDFs. Returns media=. "
         "Several: pass filename."
     ),
     "inputSchema": {"type": "object",
                     "properties": {
                         "id": {"type": "string"},
                         "filename": {"type": "string",
                                      "description": "Substring of the attachment name."},
                         "account": {"type": "string"},
                         "confirmation_token": {"type": "string"},
                     },
                     "required": ["id"]}},
    {"name": "email.draft",
     "description": "Create a DRAFT (never sends).",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "to": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "body": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["to", "subject"]}},
    {"name": "email.send_direct",
     "description": "Send a message now. Off until enabled in Connections → Email.",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "to": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "body": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["to", "subject"]}},
    {"name": "email.archive",
     "description": "Archive a message (mark seen + move to Archive folder).",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "id": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["id"]}},
    {"name": "email.flag",
     "description": "Flag or unflag a message (IMAP \\Flagged).",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "id": {"type": "string"},
                                    "remove": {"type": "boolean",
                                               "description": "true to unflag (default false)"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["id"]}},
]


DISPATCH = {
    "email.list_accounts": t_list_accounts,
    "email.list_unread": t_list_unread,
    "email.search": t_search,
    "email.fetch": t_fetch,
    "email.attachment": t_attachment,
    "email.draft": t_draft,
    "email.send_direct": t_send_direct,
    "email.archive": t_archive,
    "email.flag": t_flag,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-email", "0.4.0", TOOLS, dispatch)
