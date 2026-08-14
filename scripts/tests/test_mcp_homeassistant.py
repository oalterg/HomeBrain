"""HA MCP: camera stills go through the server, tokens never reach the agent.

The regression: ha.state returned camera `access_token` and
`entity_picture?token=...`, so OpenClaw tried to HTTP-fetch
/api/camera_proxy with that token (or the LLAT it does not have).

Run:  python3 -m pytest scripts/tests/test_mcp_homeassistant.py
"""
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SCRIPTS)

import mcp_common  # noqa: E402


def _load_ha():
    path = os.path.join(SCRIPTS, "mcp-homeassistant.py")
    spec = importlib.util.spec_from_file_location("mcp_homeassistant", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
ACCOUNT = {"name": "home", "base_url": "http://ha.local:8123", "token": "secret-llat"}


@pytest.fixture
def ha(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEBRAIN_INTEGRATIONS_KEY", "")
    monkeypatch.setenv("HOMEBRAIN_HA_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("HOMEBRAIN_AUDIT_DIR", str(tmp_path / "audit"))
    mod = _load_ha()
    monkeypatch.setattr(mod, "INTEGRATIONS_KEY", "")
    monkeypatch.setattr(mod, "_accounts", lambda: [ACCOUNT])
    return mod


# --- ha.state redaction ----------------------------------------------------

def test_state_strips_camera_tokens_and_points_at_camera_image(ha):
    ha._http = lambda *a, **k: (200, {
        "entity_id": "camera.front_door",
        "state": "idle",
        "last_changed": "2026-08-14T00:00:00+00:00",
        "attributes": {
            "friendly_name": "Front door",
            "access_token": "camera-secret",
            "entity_picture": "/api/camera_proxy/camera.front_door?token=camera-secret",
            "brand": "Reolink",
        },
    })
    out = ha.dispatch("ha.state", {"entity_id": "camera.front_door"})
    assert out["ok"] is True
    assert "access_token" not in out["attributes"]
    assert "entity_picture" not in out["attributes"]
    assert out["attributes"]["friendly_name"] == "Front door"
    assert "ha.camera_image" in out["hint"]
    assert "camera-secret" not in json.dumps(out)


def test_state_leaves_ordinary_light_attributes_alone(ha):
    ha._http = lambda *a, **k: (200, {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"brightness": 128, "friendly_name": "Kitchen"},
    })
    out = ha.dispatch("ha.state", {"entity_id": "light.kitchen"})
    assert out["ok"] is True
    assert out["attributes"]["brightness"] == 128
    assert "hint" not in out


# --- ha.camera_image -------------------------------------------------------

def test_camera_image_rejects_non_camera(ha):
    out = ha.dispatch("ha.camera_image", {"entity_id": "light.kitchen"})
    assert out["ok"] is False
    assert "not a camera" in out["error"]


def test_camera_image_fetches_via_llat_and_does_not_echo_token(ha, monkeypatch, tmp_path):
    captured = {}

    class FakeResp:
        status = 200
        headers = {"Content-Type": "image/jpeg"}

        def read(self):
            return JPEG

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(ha.urllib.request, "urlopen", fake_urlopen)
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is True
    assert captured["url"] == "http://ha.local:8123/api/camera_proxy/camera.front_door"
    assert captured["auth"] == "Bearer secret-llat"
    assert "secret-llat" not in json.dumps({k: v for k, v in out.items()
                                            if not str(k).startswith("_mcp_")})
    assert out["mime_type"] == "image/jpeg"
    assert out["path"] and os.path.isfile(out["path"])
    assert open(out["path"], "rb").read() == JPEG
    img = out["_mcp_content"][0]
    assert img["type"] == "image"
    assert img["mimeType"] == "image/jpeg"
    assert out["_mcp_media_path"] == out["path"]


def test_camera_image_404(ha):
    ha._http_bytes = lambda *a, **k: (404, b"", "")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.missing"})
    assert out["ok"] is False
    assert out["error"] == "entity not found"


def test_camera_image_rejects_oversized_still(ha):
    big = b"\xff\xd8\xff" + b"x" * (ha.CAMERA_IMAGE_MAX_BYTES + 1)
    ha._http_bytes = lambda *a, **k: (200, big, "image/jpeg")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is False
    assert "cap" in out["error"]
    assert "_mcp_content" not in out


def test_image_entity_uses_image_proxy(ha):
    seen = {}

    def fake_bytes(account, path, timeout=20):
        seen["path"] = path
        return 200, JPEG, "image/jpeg"

    ha._http_bytes = fake_bytes
    out = ha.dispatch("ha.camera_image", {"entity_id": "image.doorbell"})
    assert out["ok"] is True
    assert seen["path"] == "/api/image_proxy/image.doorbell"


# --- MCP wire shape --------------------------------------------------------

def test_tool_call_result_attaches_image_and_media_line_without_base64_in_text():
    jpeg = b"\xff\xd8\xff" + b"abc"
    envelope = {
        "ok": True,
        "entity_id": "camera.front_door",
        "path": "/tmp/camera.front_door.jpg",
        mcp_common.MCP_CONTENT: [mcp_common.mcp_image(jpeg, "image/jpeg")],
        mcp_common.MCP_MEDIA_PATH: "/tmp/camera.front_door.jpg",
    }
    result = mcp_common.tool_call_result(envelope)
    assert result["isError"] is False
    text, image = result["content"]
    assert text["type"] == "text"
    parsed = json.loads(text["text"].split("\n", 1)[0])
    assert parsed["ok"] is True
    assert "_mcp_content" not in parsed
    assert "MEDIA: /tmp/camera.front_door.jpg" in text["text"]
    assert jpeg not in text["text"].encode()
    assert image["type"] == "image"
    assert image["mimeType"] == "image/jpeg"
    assert image["data"]
