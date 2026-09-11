"""Email MCP: email.attachment writes a workspace file and returns media.

email.fetch stays text-only. Attachments go through the same path as
nc.files_download / ha.camera_image: write under the OpenClaw workspace,
return `media`, agent sends with the message tool. No bytes on the wire.

Run:  python3 -m pytest scripts/tests/test_mcp_email.py
"""
import importlib.util
import json
import os
import sys
from email.message import EmailMessage

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SCRIPTS)

import mcp_common  # noqa: E402


def _load_email():
    path = os.path.join(SCRIPTS, "mcp-email.py")
    spec = importlib.util.spec_from_file_location("mcp_email", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
PDF = b"%PDF-1.4\n%fake"
ACCOUNT = {
    "name": "home",
    "user": "a@b.c",
    "imap_host": "imap.example",
    "imap_port": 993,
    "imap_password": "secret",
}


# Live shape from Yahoo IMAP for an Apple Mail forward (PDF marked INLINE).
APPLE_MAIL_STRUCTURE = (
    b'4 (UID 7 BODYSTRUCTURE ((("text" "html" ("charset" "utf-8") NIL NIL '
    b'"quoted-printable" 65962 1288 NIL NIL NIL NIL)("application" "pdf" '
    b'("name" "invoice.pdf") NIL NIL "base64" 82810 NIL '
    b'("inline" ("filename" "invoice.pdf")) NIL NIL)("text" "html" '
    b'("charset" "us-ascii") NIL NIL "7bit" 225 0 NIL NIL NIL NIL) "mixed" '
    b'("boundary" "x") NIL) "alternative" ("boundary" "y") NIL))'
)

NEWSLETTER_STRUCTURE = (
    b'1 (BODYSTRUCTURE (("text" "html" ("charset" "utf-8") NIL NIL "7bit" '
    b'100 5 NIL NIL NIL NIL)("image" "png" ("name" "logo.png") "<cid>" NIL '
    b'"base64" 50 NIL ("inline" ("filename" "logo.png")) NIL NIL) "related"))'
)

ATTACHED_PHOTO_STRUCTURE = (
    b'1 (BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 5 NIL NIL NIL NIL)'
    b'("image" "jpeg" ("name" "photo.jpg") NIL NIL "base64" 99 NIL '
    b'("attachment" ("filename" "photo.jpg")) NIL NIL) "mixed"))'
)


class FakeIMAP:
    def __init__(self, raw, missing=False, structure=None, unseen=b"1"):
        self.raw = raw
        self.missing = missing
        self.structure = structure
        self.unseen = unseen
        self.readonly = None
        self.fetch_specs = []
        self.uid_commands = []

    def select(self, mailbox="INBOX", readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def search(self, *a, **k):
        return "OK", [self.unseen]

    def uid(self, command, *args):
        self.uid_commands.append((command,) + args)
        cmd = (command or "").upper()
        if cmd == "SEARCH":
            return self.search(None, *args)
        if cmd == "FETCH":
            return self.fetch(*args[:2])
        if cmd in ("STORE", "COPY"):
            return "OK", [b""]
        return "BAD", [b"unknown"]

    def fetch(self, uid, spec):
        spec_s = spec.decode() if isinstance(spec, bytes) else spec
        self.fetch_specs.append(spec_s)
        if self.missing:
            return "OK", [None]
        if "BODYSTRUCTURE" in spec_s:
            struct = self.structure
            if struct is None:
                struct = (
                    b'1 (BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL '
                    b'"7BIT" 5 NIL NIL NIL NIL))'
                )
            if isinstance(struct, str):
                struct = struct.encode()
            return "OK", [struct]
        return "OK", [(b"1 (RFC822)", self.raw)]

    def logout(self):
        return "BYE", []


def _msg(*attachments, body="hello"):
    msg = EmailMessage()
    msg["From"] = "school@example.com"
    msg["To"] = "a@b.c"
    msg["Subject"] = "forms"
    msg.set_content(body)
    for filename, mime, payload in attachments:
        main, sub = mime.split("/", 1)
        msg.add_attachment(payload, maintype=main, subtype=sub, filename=filename)
    return msg.as_bytes()


def _apple_fwd_pdf(filename="invoice.pdf", html="<p>fwd</p>"):
    """Apple Mail forward: HTML body + PDF with Content-Disposition: inline."""
    msg = EmailMessage()
    msg["From"] = "Oliver <o@g.com>"
    msg["To"] = "a@b.c"
    msg["Subject"] = "Fwd: Rechnung"
    msg.set_content(html, subtype="html")
    msg.add_attachment(PDF, maintype="application", subtype="pdf", filename=filename)
    for p in msg.walk():
        if p.get_content_type() == "application/pdf":
            p.replace_header("Content-Disposition", f'inline; filename="{filename}"')
    return msg


def _attach(mod, args):
    out = mod.dispatch("email.attachment", dict(args))
    if out.get("requires_confirmation"):
        args = dict(args)
        args["confirmation_token"] = out["action_id"]
        out = mod.dispatch("email.attachment", args)
    return out


def _fetch(mod, args):
    out = mod.dispatch("email.fetch", dict(args))
    if out.get("requires_confirmation"):
        args = dict(args)
        args["confirmation_token"] = out["action_id"]
        out = mod.dispatch("email.fetch", args)
    return out


@pytest.fixture
def em(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEBRAIN_EMAIL_KEY", "")
    monkeypatch.setenv("HOMEBRAIN_EMAIL_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("HOMEBRAIN_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(mcp_common, "AUDIT_DIR", str(tmp_path / "audit"))
    pending = str(tmp_path / "pending.json")
    monkeypatch.setenv("HOMEBRAIN_PENDING_ACTIONS", pending)
    monkeypatch.setattr(mcp_common.Consent, "PATH", pending)
    mod = _load_email()
    monkeypatch.setattr(mod, "KEY_B64", "")
    monkeypatch.setattr(mod, "_accounts", lambda: [ACCOUNT])
    return mod


def test_attachment_missing_id(em):
    out = em.dispatch("email.attachment", {})
    assert out["ok"] is False
    assert out["error"] == "id is required"
    assert not out.get("requires_confirmation")


def test_attachment_requires_consent_then_saves_jpeg(em):
    em._imap = lambda acc: FakeIMAP(_msg(("photo.jpg", "image/jpeg", JPEG)))
    args = {"id": "1"}
    first = em.dispatch("email.attachment", args)
    assert first.get("requires_confirmation") is True
    second = em.dispatch("email.attachment", {
        **args, "confirmation_token": first["action_id"],
    })
    assert second["ok"] is True
    assert os.path.isfile(second["path"])
    assert open(second["path"], "rb").read() == JPEG
    assert second["mime_type"] == "image/jpeg"
    assert second["filename"] == "photo.jpg"
    assert second["media"] == second["path"]
    assert "message tool" in second["hint"]
    dumped = json.dumps(second)
    assert JPEG not in dumped.encode()
    assert "base64" not in dumped
    assert "_mcp_content" not in second
    assert "_mcp_media_path" not in second
    assert "secret" not in dumped


def test_attachment_several_without_filename_returns_catalog(em, tmp_path):
    media = tmp_path / "media"
    em._imap = lambda acc: FakeIMAP(_msg(
        ("a.pdf", "application/pdf", PDF),
        ("b.pdf", "application/pdf", PDF),
    ))
    out = _attach(em, {"id": "1"})
    assert out["ok"] is True
    names = [a["filename"] for a in out["attachments"]]
    assert names == ["a.pdf", "b.pdf"]
    assert "filename" in (out.get("hint") or "")
    assert "path" not in out
    after = list(media.iterdir()) if media.exists() else []
    assert after == []


def test_attachment_filename_selects(em):
    em._imap = lambda acc: FakeIMAP(_msg(
        ("a.pdf", "application/pdf", PDF),
        ("photo.jpg", "image/jpeg", JPEG),
    ))
    out = _attach(em, {"id": "1", "filename": "photo"})
    assert out["ok"] is True
    assert out["filename"] == "photo.jpg"
    assert open(out["path"], "rb").read() == JPEG


def test_attachment_filename_miss_returns_catalog(em):
    em._imap = lambda acc: FakeIMAP(_msg(("a.pdf", "application/pdf", PDF)))
    out = _attach(em, {"id": "1", "filename": "nope"})
    assert out["ok"] is False
    assert "not found" in out["error"]
    assert out["attachments"][0]["filename"] == "a.pdf"


def test_attachment_none(em):
    em._imap = lambda acc: FakeIMAP(_msg())
    out = _attach(em, {"id": "1"})
    assert out["ok"] is False
    assert out["error"] == "no attachments"


def test_attachment_skips_inline_related_image(em):
    msg = EmailMessage()
    msg["From"] = "n@n.n"
    msg["Subject"] = "news"
    msg.set_content("hi")
    msg.add_related(JPEG, maintype="image", subtype="jpeg", filename="pixel.jpg")
    for p in msg.walk():
        if p.get_content_type() == "image/jpeg":
            p.replace_header("Content-Disposition", "inline; filename=pixel.jpg")
    em._imap = lambda acc: FakeIMAP(msg.as_bytes())
    out = _attach(em, {"id": "1"})
    assert out["ok"] is False
    assert out["error"] == "no attachments"


def test_attachment_rejects_oversize(em, tmp_path):
    big = b"\xff\xd8\xff" + b"x" * (em.MAX_ATTACHMENT_BYTES + 1)
    em._imap = lambda acc: FakeIMAP(_msg(("huge.jpg", "image/jpeg", big)))
    out = _attach(em, {"id": "1"})
    assert out["ok"] is False
    assert "cap" in out["error"]
    media = tmp_path / "media"
    after = list(media.iterdir()) if media.exists() else []
    assert after == []


def test_attachment_message_not_found(em):
    em._imap = lambda acc: FakeIMAP(b"", missing=True)
    out = _attach(em, {"id": "99"})
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_attachment_media_is_workspace_relative(em, monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    media = ws / "media" / "email"
    monkeypatch.setenv("HOMEBRAIN_OPENCLAW_WORKSPACE", str(ws))
    monkeypatch.setenv("HOMEBRAIN_EMAIL_MEDIA_DIR", str(media))
    em._imap = lambda acc: FakeIMAP(_msg(("photo.jpg", "image/jpeg", JPEG)))
    out = _attach(em, {"id": "1"})
    assert out["ok"] is True
    assert out["media"].startswith("media/email/")
    assert not out["media"].startswith("/")
    assert os.path.isfile(out["path"])


def test_attachment_wire_result_is_text_only_no_media_directive(em):
    em._imap = lambda acc: FakeIMAP(_msg(("photo.jpg", "image/jpeg", JPEG)))
    envelope = _attach(em, {"id": "1"})
    result = mcp_common.tool_call_result(envelope)
    assert result["isError"] is False
    assert len(result["content"]) == 1
    text = result["content"][0]
    assert text["type"] == "text"
    assert "MEDIA:" not in text["text"]
    parsed = json.loads(text["text"])
    assert parsed["ok"] is True
    assert parsed["media"]
    assert JPEG not in text["text"].encode()


def test_fetch_still_returns_body_not_files(em):
    em._imap = lambda acc: FakeIMAP(_msg(("photo.jpg", "image/jpeg", JPEG)))
    out = _fetch(em, {"id": "1"})
    assert out["ok"] is True
    assert "hello" in out["body"]
    assert "path" not in out
    assert "media" not in out
    assert out["attachments"][0]["filename"] == "photo.jpg"
    assert JPEG not in json.dumps(out).encode()


def test_attachment_keeps_apple_mail_inline_pdf(em):
    em._imap = lambda acc: FakeIMAP(
        _apple_fwd_pdf("Rechnung SRE10859500.pdf").as_bytes())
    out = _attach(em, {"id": "1", "filename": "SRE"})
    assert out["ok"] is True
    assert out["filename"] == "Rechnung_SRE10859500.pdf"
    assert open(out["path"], "rb").read() == PDF


def test_attachment_single_inline_pdf_needs_no_filename(em):
    em._imap = lambda acc: FakeIMAP(_apple_fwd_pdf("invoice.pdf").as_bytes())
    out = _attach(em, {"id": "1"})
    assert out["ok"] is True
    assert out["filename"] == "invoice.pdf"


def test_structure_detects_apple_mail_inline_pdf(em):
    assert em._structure_has_attachments(APPLE_MAIL_STRUCTURE) is True
    assert em._filenames_from_structure(APPLE_MAIL_STRUCTURE) == ["invoice.pdf"]
    assert em._structure_has_attachments(
        b'1 (BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 NIL NIL NIL NIL))'
    ) is False
    assert em._filenames_from_structure(NEWSLETTER_STRUCTURE) == []
    assert em._structure_has_attachments(NEWSLETTER_STRUCTURE) is False
    assert em._filenames_from_structure(ATTACHED_PHOTO_STRUCTURE) == ["photo.jpg"]
    assert em._structure_has_attachments(ATTACHED_PHOTO_STRUCTURE) is True


def test_summarise_headers_only_cannot_see_mime_parts(em):
    """IMAP RFC822.HEADER is top-level headers only — no MIME parts."""
    import email as email_mod
    full = _msg(("a.pdf", "application/pdf", PDF))
    top = email_mod.message_from_bytes(full)
    header_msg = email_mod.message.Message()
    for k, v in top.items():
        header_msg[k] = v
    header = header_msg.as_bytes()
    assert em._summarise(header, "1")["has_attachments"] is False
    assert em._summarise(header, "1")["attachments"] == []
    assert em._summarise(full, "1")["has_attachments"] is True
    assert em._summarise(full, "1")["attachments"][0]["filename"] == "a.pdf"


def test_summarise_decodes_rfc2047_subject(em):
    header = (
        b"From: =?UTF-8?Q?Oliver?= <o@g.com>\r\n"
        b"Subject: =?UTF-8?Q?F=C3=BCr_Sie?=\r\n\r\n"
    )
    s = em._summarise(header, "1")
    assert s["subject"] == "Für Sie"
    assert s["from"].startswith("Oliver")


def test_list_unread_sets_has_attachments_from_bodystructure(em):
    imap = FakeIMAP(
        _apple_fwd_pdf().as_bytes(),
        structure=APPLE_MAIL_STRUCTURE,
        unseen=b"7",
    )
    em._imap = lambda acc: imap
    out = em.dispatch("email.list_unread", {})
    assert out["ok"] is True
    assert out["messages"][0]["id"] == "7"
    assert out["messages"][0]["has_attachments"] is True
    assert out["messages"][0]["attachments"] == [{"filename": "invoice.pdf"}]
    assert imap.readonly is True
    assert any(cmd[0].upper() == "SEARCH" for cmd in imap.uid_commands)
    assert any("PEEK" in spec for spec in imap.fetch_specs)


def test_list_unread_plain_message_has_no_attachments(em):
    em._imap = lambda acc: FakeIMAP(_msg())
    out = em.dispatch("email.list_unread", {})
    assert out["ok"] is True
    assert out["messages"][0]["has_attachments"] is False
    assert out["messages"][0]["attachments"] == []


def test_fetch_html_only_forward_returns_html(em):
    imap = FakeIMAP(_apple_fwd_pdf(html="<p>Coolblue invoice</p>").as_bytes())
    em._imap = lambda acc: imap
    out = _fetch(em, {"id": "1"})
    assert out["ok"] is True
    assert "Coolblue invoice" in out["body"]
    assert out["attachments"][0]["filename"] == "invoice.pdf"
    assert imap.readonly is True
    assert any("BODY.PEEK[]" in spec for spec in imap.fetch_specs)


def test_attachment_none_is_audited(em, tmp_path):
    em._imap = lambda acc: FakeIMAP(_msg())
    out = _attach(em, {"id": "1"})
    assert out["ok"] is False
    assert out["error"] == "no attachments"
    log = (tmp_path / "audit" / "mcp-email-audit.log").read_text()
    assert "attachment.none" in log


def test_reads_use_uid_not_sequence_fetch(em):
    imap = FakeIMAP(_apple_fwd_pdf().as_bytes(), structure=APPLE_MAIL_STRUCTURE)
    em._imap = lambda acc: imap
    em.dispatch("email.list_unread", {})
    assert imap.uid_commands
    assert all(
        not spec.startswith("(RFC822")
        for spec in imap.fetch_specs
    )


def test_list_accounts_roles_no_hosts(em):
    em._accounts = lambda: [
        {"name": "Agent", "user": "agent@box.test", "agent_mailbox": True,
         "imap_host": "secret.example", "smtp_host": "smtp.example"},
        {"name": "Personal", "user": "me@box.test", "agent_mailbox": False,
         "imap_host": "imap.example"},
    ]
    desc = next(t["description"] for t in em.TOOLS
                if t["name"] == "email.list_accounts")
    assert "agent_mailbox" in desc
    assert "names only" not in desc
    out = em.dispatch("email.list_accounts", {})
    assert out["ok"] is True
    assert out["accounts"][0]["role"] == "agent_mailbox"
    assert out["accounts"][1]["role"] == "owner_inbox"
    for a in out["accounts"]:
        assert "imap_host" not in a
        assert "smtp_host" not in a
        assert "user" in a

