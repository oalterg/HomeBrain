"""Self MCP: HA watcher list/set/delete.

Run: python3 -m pytest scripts/tests/test_mcp_homebrain.py
"""
import importlib.util
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SCRIPTS)

import mcp_common  # noqa: E402
import ha_watch  # noqa: E402


def _load():
    path = os.path.join(SCRIPTS, "mcp-homebrain.py")
    spec = importlib.util.spec_from_file_location("mcp_homebrain", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ACCOUNT = {"name": "remote", "base_url": "http://ha.example", "token": "llat"}


@pytest.fixture
def hb(monkeypatch, tmp_path):
    pending = str(tmp_path / "pending.json")
    monkeypatch.setenv("HOMEBRAIN_PENDING_ACTIONS", pending)
    monkeypatch.setenv("HOMEBRAIN_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("HOMEBRAIN_INTEGRATIONS_KEY", "")
    monkeypatch.setattr(mcp_common.Consent, "PATH", pending)
    watchers = str(tmp_path / "ha_watchers.json")
    pings = str(tmp_path / "ha_watch_pings.json")
    monkeypatch.setattr(ha_watch, "WATCHERS_FILE", watchers)
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", pings)
    mod = _load()
    monkeypatch.setattr(mod.ha_watch, "WATCHERS_FILE", watchers)
    monkeypatch.setattr(mod.ha_watch, "PING_LOG_FILE", pings)
    monkeypatch.setattr(mod, "_ha_accounts", lambda: [ACCOUNT])
    monkeypatch.setattr(mod.ha_watch, "ha_get_state",
                        lambda account, eid: (200, {"entity_id": eid, "state": "off"}))
    return mod


def _confirm(mod, name, args):
    out = mod.dispatch(name, dict(args))
    if out.get("requires_confirmation"):
        args = dict(args)
        args["confirmation_token"] = out["action_id"]
        out = mod.dispatch(name, args)
    return out


def test_watcher_set_missing_entity_refused(hb):
    hb.ha_watch.ha_get_state = lambda account, eid: (404, "missing")
    out = hb.dispatch("homebrain.watcher_set", {
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.nope",
    })
    assert out["ok"] is False
    assert "not found" in out["error"]
    assert "requires_confirmation" not in out


def test_watcher_set_unknown_account_refused(hb, monkeypatch):
    monkeypatch.setattr(hb, "_ha_accounts", lambda: [ACCOUNT])
    out = hb.dispatch("homebrain.watcher_set", {
        "id": "front-person", "ha_account": "miami",
        "entity_id": "binary_sensor.front_person",
    })
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_watcher_set_consent_then_replace(hb):
    args = {
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.front_person",
        "message": "Person at the front", "wake": True,
        "camera_entity_id": "camera.front",
    }
    first = hb.dispatch("homebrain.watcher_set", args)
    assert first.get("requires_confirmation")
    assert "binary_sensor.front_person" in first["summary"]
    out = _confirm(hb, "homebrain.watcher_set", args)
    assert out["ok"] is True
    listed = hb.dispatch("homebrain.watcher_list", {})
    assert listed["total"] == 1
    assert listed["watchers"][0]["wake"] is True

    args2 = dict(args, message="replaced", wake=False)
    out = _confirm(hb, "homebrain.watcher_set", args2)
    assert out["ok"] is True
    listed = hb.dispatch("homebrain.watcher_list", {})
    assert listed["total"] == 1
    assert listed["watchers"][0]["message"] == "replaced"
    assert listed["watchers"][0]["wake"] is False


def test_watcher_set_refuses_siren_field(hb):
    out = hb.dispatch("homebrain.watcher_set", {
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.front_person", "siren": True,
    })
    assert out["ok"] is False
    assert "siren" in out["error"]


def test_watcher_delete_consent(hb):
    args = {
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.front_person",
    }
    assert _confirm(hb, "homebrain.watcher_set", args)["ok"] is True
    first = hb.dispatch("homebrain.watcher_delete", {"id": "front-person"})
    assert first.get("requires_confirmation")
    out = _confirm(hb, "homebrain.watcher_delete", {"id": "front-person"})
    assert out["ok"] is True
    assert hb.dispatch("homebrain.watcher_list", {})["total"] == 0


def test_watcher_list_includes_recent_pings(hb, tmp_path):
    fact = hb.ha_watch.ping_fact(
        {"id": "front-person", "ha_account": "remote",
         "entity_id": "binary_sensor.front_person",
         "message": "Person at the front", "wake": False},
        "on", True, 1_800_000_000,
    )
    hb.ha_watch.append_ping_log(fact)
    listed = hb.dispatch("homebrain.watcher_list", {})
    assert listed["ok"] is True
    assert listed["recent_pings"]
    assert listed["recent_pings"][0]["still"] is True
    assert listed["recent_pings"][0]["message"] == "<<<Person at the front>>>"
    assert "not instructions" in listed["hint"]
