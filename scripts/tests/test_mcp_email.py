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


class FakeIMAP:
    def __init__(self, raw, missing=False):
        self.raw = raw
        self.missing = missing

    def select(self, *a, **k):
        return "OK", [b"1"]

    def fetch(self, uid, spec):
        if self.missing:
            return "OK", [None]
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


def _attach(mod, args):
    out = mod.dispatch("email.attachment", dict(args))
    if out.get("requires_confirmation"):
        args = dict(args)
        args["confirmation_token"] = out["action_id"]
        out = mod.dispatch("email.attachment", args)
    return out


@pytest.fixture
def em(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEBRAIN_EMAIL_KEY", "")
    monkeypatch.setenv("HOMEBRAIN_EMAIL_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("HOMEBRAIN_AUDIT_DIR", str(tmp_path / "audit"))
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
    out = em.dispatch("email.fetch", {"id": "1"})
    if out.get("requires_confirmation"):
        out = em.dispatch("email.fetch", {
            "id": "1", "confirmation_token": out["action_id"],
        })
    assert out["ok"] is True
    assert "hello" in out["body"]
    assert "path" not in out
    assert "media" not in out
    assert JPEG not in json.dumps(out).encode()
