"""HA MCP: camera stills go through the server, tokens never reach the agent.

Two regressions:

1. ha.state returned camera `access_token` and `entity_picture?token=...`,
   so OpenClaw tried to HTTP-fetch /api/camera_proxy with that token (or
   the LLAT it does not have).
2. ha.camera_image then attached the JPEG as MCP image content + a MEDIA:
   path. OpenClaw ≥2026.4 truncates live tool results at ~32k chars (a
   camera still is larger, so the call looks like an error), strips local
   MEDIA: from MCP tools, and does not relay ImageContent to Telegram. The
   qwen agent spent long turns on the truncated payload, tried to decrypt
   the HA credential, and fell through to camera.snapshot — which writes
   the JPEG on the *remote* HA instance.

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
    assert "token" not in out["hint"].lower()


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


def test_camera_image_fetches_via_llat_and_does_not_echo_token(ha, monkeypatch):
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
        captured["accept"] = req.get_header("Accept")
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(ha.urllib.request, "urlopen", fake_urlopen)
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is True
    assert captured["url"] == "http://ha.local:8123/api/camera_proxy/camera.front_door"
    assert captured["auth"] == "Bearer secret-llat"
    assert "image/jpeg" in (captured["accept"] or "")
    assert captured["timeout"] == ha.CAMERA_IMAGE_TIMEOUT
    dumped = json.dumps(out)
    assert "secret-llat" not in dumped
    assert JPEG not in dumped.encode()
    assert "_mcp_content" not in out
    assert "_mcp_media_path" not in out
    assert out["mime_type"] == "image/jpeg"
    assert out["path"] and os.path.isfile(out["path"])
    assert open(out["path"], "rb").read() == JPEG
    assert out["media"] == out["path"]  # tmp media dir is outside workspace
    assert "message tool" in out["hint"]
    assert "THIS HomeBrain" in out["hint"]
    assert "camera.snapshot" in out["hint"]


def test_camera_image_404(ha):
    ha._http_bytes = lambda *a, **k: (404, b"", "")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.missing"})
    assert out["ok"] is False
    assert out["error"] == "entity not found"


def test_camera_image_400_does_not_invite_snapshot_or_tokens(ha):
    ha._http_bytes = lambda *a, **k: (
        400, b'{"message": "Unable to get image"}', "")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is False
    assert "HTTP 400" in out["error"]
    assert "Unable to get image" in out["error"]
    assert "ha.camera_image" in out["hint"]
    assert "camera.snapshot" in out["hint"]
    assert "decrypt" in out["hint"]
    dumped = json.dumps(out).lower()
    assert "secret-llat" not in dumped


def test_camera_image_500_is_unavailable_and_strips_html(ha):
    ha._http_bytes = lambda *a, **k: (
        500, b"<html><h1>Internal Server Error</h1></html>", "")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is False
    assert out.get("unavailable") is True
    assert "HTTP 500" in out["hint"]
    assert "<html>" not in json.dumps(out)
    assert "decrypt" in out["hint"]


def test_camera_image_400_strips_credential_shaped_bodies(ha):
    ha._http_bytes = lambda *a, **k: (
        400, b'{"message": "Invalid bearer token gAAAAA-not-a-real-secret"}', "")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    dumped = json.dumps(out)
    assert "gAAAAA" not in dumped
    assert "bearer token" not in dumped.lower()
    assert "HTTP 400" in out["error"]


def test_camera_image_rejects_oversized_still(ha):
    big = b"\xff\xd8\xff" + b"x" * (ha.CAMERA_IMAGE_MAX_BYTES + 1)
    ha._http_bytes = lambda *a, **k: (200, big, "image/jpeg")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is False
    assert "cap" in out["error"]
    assert "_mcp_content" not in out


def test_image_entity_uses_image_proxy(ha):
    seen = {}

    def fake_bytes(account, path, timeout=45):
        seen["path"] = path
        return 200, JPEG, "image/jpeg"

    ha._http_bytes = fake_bytes
    out = ha.dispatch("ha.camera_image", {"entity_id": "image.doorbell"})
    assert out["ok"] is True
    assert seen["path"] == "/api/image_proxy/image.doorbell"


def test_camera_image_extracts_jpeg_from_mjpeg(ha):
    frame = JPEG + b"\xff\xd9"
    wrapped = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n--frame--"
    ha._http_bytes = lambda *a, **k: (200, wrapped, "multipart/x-mixed-replace")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is True
    assert out["mime_type"] == "image/jpeg"
    assert open(out["path"], "rb").read() == frame


def test_camera_image_media_is_workspace_relative(ha, monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    media = ws / "media"
    monkeypatch.setenv("HOMEBRAIN_OPENCLAW_WORKSPACE", str(ws))
    monkeypatch.setenv("HOMEBRAIN_HA_MEDIA_DIR", str(media))
    ha._http_bytes = lambda *a, **k: (200, JPEG, "image/jpeg")
    out = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
    assert out["ok"] is True
    assert out["media"] == "media/camera.front_door.jpg"
    assert os.path.isfile(out["path"])


def test_call_service_refuses_camera_snapshot(ha):
    out = ha.dispatch("ha.call_service", {
        "domain": "camera", "service": "snapshot",
        "target": {"entity_id": "camera.front_door"},
        "account": "home",
    })
    assert out["ok"] is False
    assert "ha.camera_image" in out["error"]
    assert "Home Assistant instance" in out["hint"]


def test_call_service_raw_refuses_camera_snapshot(ha):
    out = ha.dispatch("ha.call_service_raw", {
        "domain": "camera", "service": "snapshot",
        "target": {"entity_id": "camera.front_door"},
        "account": "home",
    })
    assert out["ok"] is False
    assert "ha.camera_image" in out["error"]
    assert "confirmation_token" not in out  # refused before consent


# --- MCP wire shape --------------------------------------------------------

def test_camera_image_wire_result_is_text_only_no_media_directive(ha):
    ha._http_bytes = lambda *a, **k: (200, JPEG, "image/jpeg")
    envelope = ha.dispatch("ha.camera_image", {"entity_id": "camera.front_door"})
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


def test_list_services_requires_domain(ha):
    ha._http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not hit HA"))
    out = ha.dispatch("ha.list_services", {"account": "home"})
    assert out["ok"] is False
    assert out["error"] == "domain is required"


def test_tool_descriptions_stay_short_for_the_model(ha):
    for tool in ha.TOOLS:
        assert len(tool["description"]) <= 160, tool["name"]


def test_todo_and_tts_are_allowlisted(ha):
    assert "todo" in ha.SERVICE_DOMAIN_ALLOWLIST
    assert "tts" in ha.SERVICE_DOMAIN_ALLOWLIST


def test_area_list_returns_id_and_name(ha):
    ha._http = lambda *a, **k: (200, [
        {"id": "kitchen", "name": "Kitchen"},
        {"id": "living_room", "name": "Living room"},
    ])
    out = ha.dispatch("ha.area_list", {})
    assert out["ok"] is True
    assert out["areas"] == [
        {"id": "kitchen", "name": "Kitchen"},
        {"id": "living_room", "name": "Living room"},
    ]


def test_area_list_accepts_legacy_id_strings(ha):
    ha._http = lambda *a, **k: (200, '["kitchen","hall"]')
    out = ha.dispatch("ha.area_list", {})
    assert out["ok"] is True
    assert out["areas"] == [
        {"id": "kitchen", "name": "kitchen"},
        {"id": "hall", "name": "hall"},
    ]


def test_calendar_events_one_entity(ha):
    seen = {}

    def fake(account, method, path, body=None, timeout=8):
        seen["path"] = path
        return 200, [{
            "summary": "Dentist",
            "start": {"dateTime": "2026-08-15T08:00:00+02:00"},
            "end": {"dateTime": "2026-08-15T09:00:00+02:00"},
            "location": "Berlin",
            "description": "Bring card",
        }]

    ha._http = fake
    out = ha.dispatch("ha.calendar_events", {"entity_id": "calendar.family"})
    assert out["ok"] is True
    assert "calendar.family" in seen["path"]
    assert "start=" in seen["path"]
    ev = out["calendars"][0]["events"][0]
    assert ev["summary"] == "Dentist"
    assert ev["start"].startswith("2026-08-15")
    assert ev["location"] == "Berlin"
    assert out["total"] == 1


def test_calendar_events_all_calendars(ha):
    def fake(account, method, path, body=None, timeout=8):
        if path == "/api/calendars":
            return 200, [
                {"entity_id": "calendar.family", "name": "Family"},
                {"entity_id": "calendar.work", "name": "Work"},
            ]
        if "calendar.family" in path:
            return 200, [{"summary": "Dinner", "start": {"date": "2026-08-15"},
                          "end": {"date": "2026-08-16"}}]
        return 200, []

    ha._http = fake
    out = ha.dispatch("ha.calendar_events", {})
    assert out["ok"] is True
    names = {c["name"] for c in out["calendars"]}
    assert names == {"Family", "Work"}
    family = next(c for c in out["calendars"] if c["name"] == "Family")
    assert family["events"][0]["summary"] == "Dinner"


def test_calendar_events_rejects_non_calendar_entity(ha):
    ha._http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not hit HA"))
    out = ha.dispatch("ha.calendar_events", {"entity_id": "light.kitchen"})
    assert out["ok"] is False
    assert "calendar.*" in out["error"]
