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
  * REVEAL    : email.fetch — full body. Audited. Consent-gated.
  * ACT       : email.draft (creates a draft, never sends), email.archive,
                email.flag. Consent-gated. email.send_direct is OFF by
                default (gated behind a settings-level toggle the dashboard
                sets via env HOMEBRAIN_EMAIL_SEND_DIRECT=true).

If HOMEBRAIN_EMAIL_KEY is not set, accounts are assumed to be stored in
plaintext (development mode). The dashboard always sets it on the live box.
"""
from __future__ import annotations

import base64
import email
import imaplib
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

ACCOUNTS_FILE = os.environ.get(
    "HOMEBRAIN_EMAIL_ACCOUNTS",
    os.path.expanduser("~/.openclaw/email_accounts.json"),
)
KEY_B64 = os.environ.get("HOMEBRAIN_EMAIL_KEY", "")
SEND_DIRECT_ENABLED = os.environ.get("HOMEBRAIN_EMAIL_SEND_DIRECT", "false").lower() == "true"


def _decrypt(blob: str) -> str:
    if not KEY_B64:
        return blob  # development / unencrypted mode
    try:
        from cryptography.fernet import Fernet  # type: ignore
        return Fernet(KEY_B64.encode()).decrypt(blob.encode()).decode()
    except Exception:
        return ""


def _accounts() -> list[dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("accounts", []) if isinstance(data, dict) else []


def _find(name: str) -> dict | None:
    for a in _accounts():
        if a.get("name") == name:
            return a
    return None


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
    accounts = [{"name": a.get("name"),
                 "user": a.get("user"),
                 "imap_host": a.get("imap_host"),
                 "smtp_host": a.get("smtp_host")} for a in _accounts()]
    return ok(accounts=accounts, total=len(accounts),
              send_direct_enabled=SEND_DIRECT_ENABLED)


def _summarise(msg_bytes: bytes, uid: str) -> dict:
    msg = email.message_from_bytes(msg_bytes)
    received = ""
    if msg.get("Date"):
        try:
            received = parsedate_to_datetime(msg["Date"]).isoformat()
        except (TypeError, ValueError):
            pass
    return {
        "id": uid,
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
        "subject": msg.get("Subject", ""),
        "received": received,
        "has_attachments": any(
            (part.get_content_disposition() == "attachment")
            for part in msg.walk()
        ),
    }


def t_list_unread(args: dict) -> dict:
    name = args.get("account") or ""
    limit = int(args.get("limit") or 20)
    acc = _find(name)
    if not acc:
        return err(f"account '{name}' not found")
    conn = _imap(acc)
    if not conn:
        return unavailable(f"could not connect to IMAP for '{name}'")
    try:
        conn.select("INBOX", readonly=True)
        rc, ids = conn.search(None, "UNSEEN")
        if rc != "OK" or not ids or not ids[0]:
            return ok(account=name, messages=[], total=0)
        uids = ids[0].split()[-limit:]
        out = []
        for uid in reversed(uids):
            rc, data = conn.fetch(uid, "(RFC822.HEADER)")
            if rc != "OK" or not data or not data[0]:
                continue
            out.append(_summarise(data[0][1], uid.decode()))
        return ok(account=name, messages=out, total=len(out))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def t_search(args: dict) -> dict:
    name = args.get("account") or ""
    query = args.get("query") or ""
    limit = int(args.get("limit") or 30)
    acc = _find(name)
    if not acc:
        return err(f"account '{name}' not found")
    if not query:
        return err("query is required")
    conn = _imap(acc)
    if not conn:
        return unavailable(f"could not connect to IMAP for '{name}'")
    try:
        conn.select("INBOX", readonly=True)
        # IMAP TEXT search — matches headers + body; returns IDs only.
        rc, ids = conn.search(None, "TEXT", f'"{query}"')
        if rc != "OK" or not ids or not ids[0]:
            return ok(account=name, messages=[], total=0)
        uids = ids[0].split()[-limit:]
        out = []
        for uid in reversed(uids):
            rc, data = conn.fetch(uid, "(RFC822.HEADER)")
            if rc != "OK" or not data or not data[0]:
                continue
            out.append(_summarise(data[0][1], uid.decode()))
        return ok(account=name, messages=out, total=len(out))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def t_fetch(args: dict) -> dict:
    """REVEAL-tier: pull a full message body."""
    name = args.get("account") or ""
    msg_id = args.get("id") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not name or not msg_id:
        return err("account and id are required")
    summary = f"Email: read full body of message {msg_id} from account '{name}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _find(redeemed["account"])
    if not acc:
        return err("account not found")
    conn = _imap(acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX")
        rc, data = conn.fetch(redeemed["id"].encode(), "(RFC822)")
        if rc != "OK" or not data or not data[0]:
            return err("message not found")
        msg = email.message_from_bytes(data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain" and part.get_content_disposition() != "attachment":
                    try:
                        body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    except Exception:
                        body = part.get_payload()
                    break
        else:
            try:
                body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
            except Exception:
                body = msg.get_payload()
        audit("email", "fetch", account=redeemed["account"], id=redeemed["id"])
        return ok(
            id=redeemed["id"],
            from_=msg.get("From", ""),
            to=msg.get("To", ""),
            subject=msg.get("Subject", ""),
            received=msg.get("Date", ""),
            body=body[:50_000],  # hard cap to keep token usage sane
            truncated=len(body) > 50_000,
        )
    finally:
        try:
            conn.logout()
        except Exception:
            pass


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
    """ACT-tier: create a draft. Drafts are reversible; sends are not.
    The agent CANNOT directly send — that's email.send_direct, gated."""
    name = args.get("account") or ""
    to = args.get("to") or ""
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not all([name, to, subject]):
        return err("account, to, subject required")
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
    acc = _find(redeemed["account"])
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
    """ACT+ tier: send mail directly. Off by default; requires the
    dashboard to opt-in via HOMEBRAIN_EMAIL_SEND_DIRECT=true."""
    if not SEND_DIRECT_ENABLED:
        return err(
            "email.send_direct is disabled",
            hint="Enable in HomeBrain dashboard → Connections → Email → Settings",
        )
    name = args.get("account") or ""
    to = args.get("to") or ""
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not all([name, to, subject]):
        return err("account, to, subject required")
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
    acc = _find(redeemed["account"])
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
    """ACT-tier (low risk): mark seen + move to Archive folder."""
    name = args.get("account") or ""
    msg_id = args.get("id") or ""
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not name or not msg_id:
        return err("account and id required")
    summary = f"Email: archive message {msg_id} on account '{name}'"
    if not confirm:
        action_id = Consent.issue("email", summary,
                                  {"account": name, "id": msg_id},
                                  chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "email", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    acc = _find(redeemed["account"])
    if not acc:
        return err("account not found")
    conn = _imap(acc)
    if not conn:
        return unavailable("could not connect to IMAP")
    try:
        conn.select("INBOX")
        archive = acc.get("archive_folder") or "Archive"
        try:
            conn.create(archive)
        except Exception:
            pass
        conn.copy(redeemed["id"].encode(), archive)
        conn.store(redeemed["id"].encode(), "+FLAGS", r"(\Deleted \Seen)")
        conn.expunge()
        audit("email", "archive", account=redeemed["account"], id=redeemed["id"])
        return ok(archived=True, folder=archive)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


TOOLS = [
    {"name": "email.list_accounts",
     "description": "List configured email accounts (names only — never credentials).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "email.list_unread",
     "description": "List unread messages from one account. Returns headers only (from, subject, date) — never bodies.",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["account"]}},
    {"name": "email.search",
     "description": "IMAP TEXT search across one account. Returns headers only.",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["account", "query"]}},
    {"name": "email.fetch",
     "description": "REVEAL-tier: fetch a full message body. Requires consent token. Audited.",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "id": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["account", "id"]}},
    {"name": "email.draft",
     "description": "ACT-tier: create a DRAFT (never sends). Requires consent token.",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "to": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "body": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["account", "to", "subject"]}},
    {"name": "email.send_direct",
     "description": (
         "ACT+ tier: send a message directly. Disabled by default. Enable "
         "in HomeBrain dashboard → Connections → Email → Settings. Requires "
         "consent token even when enabled."
     ),
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "to": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "body": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["account", "to", "subject"]}},
    {"name": "email.archive",
     "description": "ACT-tier: archive a message (mark seen + move to Archive folder).",
     "inputSchema": {"type": "object",
                     "properties": {"account": {"type": "string"},
                                    "id": {"type": "string"},
                                    "confirmation_token": {"type": "string"}},
                     "required": ["account", "id"]}},
]


DISPATCH = {
    "email.list_accounts": t_list_accounts,
    "email.list_unread": t_list_unread,
    "email.search": t_search,
    "email.fetch": t_fetch,
    "email.draft": t_draft,
    "email.send_direct": t_send_direct,
    "email.archive": t_archive,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-email", "0.1.0", TOOLS, dispatch)
