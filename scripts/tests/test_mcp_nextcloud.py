"""NC MCP: files_download writes a workspace file and returns a media path.

OpenClaw ≥2026.4 truncates live tool results at ~32–64k chars, strips
`MEDIA:` from MCP tools (GHSA-jjgj-cpp9-cvpv), and does not relay
ImageContent to Telegram. Returning UTF-8 / base64 bytes in the envelope
therefore never reaches the chat. The HA camera path already fixed this:
write a file under the OpenClaw workspace and return `media`; the agent
sends it with the message tool.

`nc.files_download` is REVEAL-tier. `dispatch()` does not auto-confirm.

Run:  python3 -m pytest scripts/tests/test_mcp_nextcloud.py
"""
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SCRIPTS)

import mcp_common  # noqa: E402


def _load_nc():
    path = os.path.join(SCRIPTS, "mcp-nextcloud.py")
    spec = importlib.util.spec_from_file_location("mcp_nextcloud", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake"
PDF = b"%PDF-1.4\n%fake"
ACCOUNT = {
    "name": "home",
    "base_url": "http://nc.local",
    "user": "alice",
    "token": "app-secret",
}


class FakeResp:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, n=-1):
        if n is None or n < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _download(mod, args):
    out = mod.dispatch("nc.files_download", dict(args))
    if out.get("requires_confirmation"):
        args = dict(args)
        args["confirmation_token"] = out["action_id"]
        out = mod.dispatch("nc.files_download", args)
    return out


def _must_not_get(*a, **k):
    pytest.fail("GET must not run")


def _stub_head_then_file(mod, body, mime):
    """HEAD via `_http`; GET writes `body` to dest_path via `_http_get_capped`."""
    def fake_http(*a, **k):
        return 200, b"", {
            "Content-Length": str(len(body)),
            "Content-Type": mime,
        }

    def fake_get(*args, **kwargs):
        dest_path = kwargs.get("dest_path", args[2] if len(args) > 2 else None)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(body)
        return 200, len(body), {"Content-Type": mime}

    mod._http = fake_http
    mod._http_get_capped = fake_get


@pytest.fixture
def nc(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEBRAIN_INTEGRATIONS_KEY", "")
    monkeypatch.setenv("HOMEBRAIN_NC_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("HOMEBRAIN_AUDIT_DIR", str(tmp_path / "audit"))
    pending = str(tmp_path / "pending.json")
    monkeypatch.setenv("HOMEBRAIN_PENDING_ACTIONS", pending)
    monkeypatch.setattr(mcp_common.Consent, "PATH", pending)
    mod = _load_nc()
    monkeypatch.setattr(mod, "INTEGRATIONS_KEY", "")
    monkeypatch.setattr(mod, "_accounts", lambda: [ACCOUNT])
    return mod


# --- constants -------------------------------------------------------------

def test_download_module_constants(nc):
    assert nc.MAX_DOWNLOAD_BYTES == 20_000_000
    assert nc.TEXT_INGEST_MAX == 20_000


# --- validation before HTTP / consent --------------------------------------

def test_download_missing_path(nc):
    nc._http = lambda *a, **k: pytest.fail("must not hit Nextcloud")
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {})
    assert out["ok"] is False
    assert out["error"] == "path is required"
    assert not out.get("requires_confirmation")


def test_download_rejects_dotdot_before_http(nc):
    nc._http = lambda *a, **k: pytest.fail("must not hit Nextcloud")
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/foo/../secret.txt"})
    assert out["ok"] is False
    assert "invalid" in out["error"]
    assert not out.get("requires_confirmation")


def test_download_rejects_root_as_folder(nc):
    nc._http = lambda *a, **k: (200, b"", {"Content-Type": "httpd/unix-directory"})
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/"})
    assert out["ok"] is False
    assert "folder" in out["error"]
    assert not out.get("requires_confirmation")


def test_download_rejects_folder_via_head(nc):
    nc._http = lambda *a, **k: (200, b"", {"Content-Type": "httpd/unix-directory"})
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/Photos"})
    assert out["ok"] is False
    assert "folder" in out["error"]
    assert not out.get("requires_confirmation")


def test_download_404_before_consent(nc):
    nc._http = lambda *a, **k: (404, b"", {})
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/Photos/missing.jpg"})
    assert out["ok"] is False
    assert not out.get("requires_confirmation")
    err = (out.get("error") or "").lower()
    assert "404" in err or "not found" in err


def test_download_oversize_via_head_does_not_get(nc):
    nc._http = lambda *a, **k: (200, b"", {
        "Content-Length": str(nc.MAX_DOWNLOAD_BYTES + 1),
        "Content-Type": "image/jpeg",
    })
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/Photos/huge.jpg"})
    assert out["ok"] is False
    assert not out.get("requires_confirmation")
    assert "cap" in out["error"]
    assert "nc.files_share" in (out.get("hint") or out.get("error") or "")


def test_download_streamed_oversize_no_leftover(nc, tmp_path):
    media = tmp_path / "media"

    nc._http = lambda *a, **k: (405, b"", {})

    def fake_get(*args, **kwargs):
        dest_path = kwargs.get("dest_path", args[2] if len(args) > 2 else None)
        if dest_path and os.path.exists(dest_path):
            os.remove(dest_path)
        return 200, nc.MAX_DOWNLOAD_BYTES + 1, {}

    nc._http_get_capped = fake_get
    before = set(os.listdir(media)) if media.exists() else set()
    # HEAD 405 cannot see the size, so consent may run; cap is enforced on GET.
    out = _download(nc, {"path": "/Photos/huge.bin"})
    assert out["ok"] is False
    assert "cap" in out["error"]
    after = set(os.listdir(media)) if media.exists() else set()
    assert after == before


# --- consent ---------------------------------------------------------------

def test_download_requires_consent_then_succeeds(nc):
    _stub_head_then_file(nc, JPEG, "image/jpeg")
    args = {"path": "/Photos/kitchen.jpg"}
    first = nc.dispatch("nc.files_download", args)
    assert first.get("requires_confirmation") is True
    assert first.get("action_id")
    second = nc.dispatch("nc.files_download", {
        **args, "confirmation_token": first["action_id"],
    })
    assert second["ok"] is True
    assert os.path.isfile(second["path"])


# --- success envelope ------------------------------------------------------

def test_download_jpeg_writes_media_not_base64(nc):
    _stub_head_then_file(nc, JPEG, "image/jpeg")
    out = _download(nc, {"path": "/Photos/kitchen.jpg"})
    assert out["ok"] is True
    assert out["account"] == "home"
    assert out["filename"] == "kitchen.jpg"
    assert out["mime_type"] == "image/jpeg"
    assert out["size"] == len(JPEG)
    assert out["path"] and os.path.isfile(out["path"])
    assert open(out["path"], "rb").read() == JPEG
    assert out["media"] == out["path"]  # tmp media dir is outside workspace
    assert "message tool" in out["hint"]
    assert "THIS HomeBrain" in out["hint"]
    assert "nc.files_share" in out["hint"]
    dumped = json.dumps(out)
    assert JPEG not in dumped.encode()
    assert "base64" not in dumped
    assert "_mcp_content" not in out
    assert "_mcp_media_path" not in out
    assert "app-secret" not in dumped
    assert "content" not in out


def test_download_png_sniffs_mime(nc):
    _stub_head_then_file(nc, PNG, "application/octet-stream")
    out = _download(nc, {"path": "/Photos/shot.png"})
    assert out["ok"] is True
    assert out["mime_type"] == "image/png"
    assert open(out["path"], "rb").read() == PNG
    assert "content" not in out


def test_download_pdf_sniffs_mime(nc):
    _stub_head_then_file(nc, PDF, "application/octet-stream")
    out = _download(nc, {"path": "/Docs/note.pdf"})
    assert out["ok"] is True
    assert out["mime_type"] == "application/pdf"
    assert open(out["path"], "rb").read() == PDF
    dumped = json.dumps(out)
    assert "content" not in out
    assert "base64" not in dumped
    assert "app-secret" not in dumped


def test_download_small_text_ingests_utf8_and_writes_media(nc):
    text = "hello notes\n"
    _stub_head_then_file(nc, text.encode("utf-8"), "text/plain")
    out = _download(nc, {"path": "/Notes/hello.txt"})
    assert out["ok"] is True
    assert out["encoding"] == "utf-8"
    assert out["content"] == text
    assert out["path"] and os.path.isfile(out["path"])
    assert open(out["path"], "rb").read() == text.encode("utf-8")
    assert out["media"]
    dumped = json.dumps(out)
    assert "base64" not in dumped
    assert "app-secret" not in dumped


def test_download_long_text_omits_content(nc):
    text = "x" * (nc.TEXT_INGEST_MAX + 1)
    _stub_head_then_file(nc, text.encode("utf-8"), "text/plain")
    out = _download(nc, {"path": "/Notes/long.txt"})
    assert out["ok"] is True
    assert "content" not in out
    assert out["path"] and os.path.isfile(out["path"])
    assert open(out["path"], "rb").read() == text.encode("utf-8")
    assert out["media"]


def test_download_media_is_workspace_relative(nc, monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    media = ws / "media" / "nextcloud"
    monkeypatch.setenv("HOMEBRAIN_OPENCLAW_WORKSPACE", str(ws))
    monkeypatch.setenv("HOMEBRAIN_NC_MEDIA_DIR", str(media))
    _stub_head_then_file(nc, JPEG, "image/jpeg")
    out = _download(nc, {"path": "/Photos/kitchen.jpg"})
    assert out["ok"] is True
    assert out["media"].startswith("media/nextcloud/")
    assert not out["media"].startswith("/")
    assert os.path.isfile(out["path"])


# --- HTTP errors -----------------------------------------------------------

def test_download_401_unavailable(nc):
    nc._http = lambda *a, **k: (401, b"", {})
    nc._http_get_capped = _must_not_get
    out = _download(nc, {"path": "/Photos/kitchen.jpg"})
    assert out["ok"] is False
    assert out.get("unavailable") is True
    assert "app-secret" not in json.dumps(out)


def test_download_403_hint_no_token(nc):
    nc._http = lambda *a, **k: (403, b"", {})
    nc._http_get_capped = _must_not_get
    out = nc.dispatch("nc.files_download", {"path": "/Photos/kitchen.jpg"})
    assert out["ok"] is False
    hint = (out.get("hint") or out.get("error") or "").lower()
    assert "encrypted" in hint or "access" in hint
    assert "app-secret" not in json.dumps(out)


# --- path quoting / Basic auth ---------------------------------------------

def test_download_quotes_path_and_uses_basic_auth(nc, monkeypatch):
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append({
            "method": req.get_method(),
            "url": req.full_url,
            "auth": req.get_header("Authorization"),
        })
        if req.get_method() == "HEAD":
            return FakeResp(200, {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(JPEG)),
            }, b"")
        return FakeResp(200, {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(JPEG)),
        }, JPEG)

    monkeypatch.setattr(nc.urllib.request, "urlopen", fake_urlopen)
    out = _download(nc, {"path": "/Photos/my photo.jpg"})
    assert out["ok"] is True
    expected = "http://nc.local/remote.php/dav/files/alice/Photos/my%20photo.jpg"
    gets = [c for c in captured if c["method"] == "GET"]
    assert gets, captured
    assert gets[0]["url"] == expected
    assert gets[0]["auth"] and "Basic" in gets[0]["auth"]
    dumped = json.dumps(out)
    assert "app-secret" not in dumped
    assert JPEG not in dumped.encode()


# --- MCP wire shape --------------------------------------------------------

def test_download_wire_result_is_text_only_no_media_directive(nc):
    _stub_head_then_file(nc, JPEG, "image/jpeg")
    envelope = _download(nc, {"path": "/Photos/kitchen.jpg"})
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


# --- DAV href unquote ------------------------------------------------------

_PROPFIND_ENCODED = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:">'
    '<d:response><d:href>/remote.php/dav/files/alice/Documents</d:href>'
    '<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>'
    '<d:getcontentlength/><d:getlastmodified/></d:prop></d:propstat></d:response>'
    '<d:response><d:href>/remote.php/dav/files/alice/Documents/Nextcloud%20flyer.pdf</d:href>'
    '<d:propstat><d:prop><d:resourcetype/><d:getcontentlength>1083339</d:getcontentlength>'
    '<d:getlastmodified/></d:prop></d:propstat></d:response>'
    '</d:multistatus>'
).encode()


def test_files_list_unquotes_dav_href(nc):
    nc._http = lambda *a, **k: (207, _PROPFIND_ENCODED, {})
    out = nc.dispatch("nc.files_list", {"path": "/Documents"})
    assert out["ok"] is True
    paths = [e["path"] for e in out["entries"]]
    assert "/Documents/Nextcloud flyer.pdf" in paths
    assert "/Documents/Nextcloud%20flyer.pdf" not in paths
    names = [e["name"] for e in out["entries"]]
    assert "Nextcloud flyer.pdf" in names


def test_files_search_unquotes_dav_href(nc):
    nc._http = lambda *a, **k: (207, _PROPFIND_ENCODED, {})
    out = nc.dispatch("nc.files_search", {"query": "flyer"})
    assert out["ok"] is True
    paths = [e["path"] for e in out["results"]]
    assert "/Documents/Nextcloud flyer.pdf" in paths
    assert "/Documents/Nextcloud%20flyer.pdf" not in paths
