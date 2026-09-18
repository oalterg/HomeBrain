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
    monkeypatch.setattr(ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
    mod = _load()
    monkeypatch.setattr(mod.ha_watch, "WATCHERS_FILE", watchers)
    monkeypatch.setattr(mod.ha_watch, "PING_LOG_FILE", pings)
    monkeypatch.setattr(mod.ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
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
    assert "cooldown_s" not in listed["watchers"][0] if listed["watchers"] else True


def test_watcher_set_omitted_id_and_ignored_knobs(hb):
    args = {
        "ha_account": "remote",
        "entity_id": "binary_sensor.front_person",
        "message": "Person at the front",
        "cooldown_s": 60,
        "enabled": False,
    }
    out = _confirm(hb, "homebrain.watcher_set", args)
    assert out["ok"] is True
    w = out["watcher"]
    assert w["id"] == "remote-binary-sensor-front-person"
    assert "cooldown_s" not in w
    assert "enabled" not in w
    on_disk = hb.ha_watch.load_watchers()[0]
    assert on_disk["cooldown_s"] == 120
    assert on_disk["enabled"] is True


def test_watcher_set_same_pair_replaces_other_id(hb):
    first = _confirm(hb, "homebrain.watcher_set", {
        "id": "e2e-watch", "ha_account": "remote",
        "entity_id": "binary_sensor.front_person", "message": "one",
    })
    assert first["ok"] is True
    second = _confirm(hb, "homebrain.watcher_set", {
        "ha_account": "remote",
        "entity_id": "binary_sensor.front_person", "message": "two",
    })
    assert second["ok"] is True
    assert second["watcher"]["id"] == "e2e-watch"
    listed = hb.dispatch("homebrain.watcher_list", {})
    assert listed["total"] == 1
    assert listed["watchers"][0]["message"] == "two"


def test_watcher_delete_prunes_runtime_state(hb):
    hb.ha_watch.save_runtime_state({
        "front-person": {"last_state": "on", "last_fired": 1},
        "other": {"last_state": "off", "last_fired": 0},
    })
    args = {
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.front_person",
    }
    assert _confirm(hb, "homebrain.watcher_set", args)["ok"] is True
    assert _confirm(hb, "homebrain.watcher_delete", {"id": "front-person"})["ok"] is True
    state = hb.ha_watch.load_runtime_state()
    assert "front-person" not in state
    assert "other" not in state


def test_setup_skip_does_not_execute_on_the_first_call(hb, monkeypatch):
    calls = []

    def capture(method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 200, {"status": "ok", "remaining": []}

    monkeypatch.setattr(hb, "_http", capture)
    monkeypatch.setenv("HOMEBRAIN_MCP_CONSENT", "false")
    first = hb.dispatch("homebrain.setup_skip", {"step": "offsite"})
    assert first.get("requires_confirmation") is True
    assert first.get("no_auto_confirm") is True
    assert calls == []
    redeemed = mcp_common.maybe_auto_confirm(
        hb.dispatch, "homebrain.setup_skip", {"step": "offsite"}, first)
    assert redeemed.get("requires_confirmation") is True
    assert calls == []


def test_setup_skip_executes_only_after_a_second_call(hb, monkeypatch):
    calls = []

    def capture(method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 200, {"status": "ok", "skipped": ["offsite"]}

    monkeypatch.setattr(hb, "_http", capture)
    first = hb.dispatch("homebrain.setup_skip", {"step": "offsite"})
    out = hb.dispatch("homebrain.setup_skip", {
        "step": "phone",
        "confirmation_token": first["action_id"],
    })
    assert out.get("ok") is True
    assert calls == [("POST", "/api/integrations/self/skip", {"step": "offsite"})]


def test_setup_status_is_a_read(hb, monkeypatch):
    monkeypatch.setattr(hb, "_http", lambda method, path, body=None, timeout=10: (
        200, {"complete": False, "has_gpu": True,
              "remaining": [{"id": "telegram"}], "skipped": []}))
    out = hb.dispatch("homebrain.setup_status", {})
    assert out["ok"] is True
    assert out["next"] == "telegram"
    assert out["complete"] is False
    assert "dashboard-only" in out["hint"]


def test_setup_status_offers_a_tool_when_next_is_backup(hb, monkeypatch):
    monkeypatch.setattr(hb, "_http", lambda method, path, body=None, timeout=10: (
        200, {"complete": False, "has_gpu": True,
              "remaining": [{"id": "backup"}], "skipped": []}))
    out = hb.dispatch("homebrain.setup_status", {})
    assert out["next"] == "backup"
    assert "homebrain.*" in out["hint"]
    assert "dashboard-only" not in out["hint"]


def test_setup_skip_fails_closed_when_redeemed_payload_has_no_step(hb, monkeypatch):
    calls = []

    def capture(method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 200, {"status": "ok"}

    monkeypatch.setattr(hb, "_http", capture)
    monkeypatch.setattr(hb.Consent, "verify", lambda *a, **k: {})
    out = hb.dispatch("homebrain.setup_skip", {
        "step": "phone", "confirmation_token": "tok",
    })
    assert out.get("ok") is False
    assert calls == []


def test_ca_info_does_not_return_the_pem(hb, monkeypatch):
    monkeypatch.setattr(hb, "_http", lambda method, path, body=None, timeout=10: (
        200, {"ok": True, "pem": "-----BEGIN CERTIFICATE-----\nMII\n",
              "names": ["homebrain.local"],
              "download": "/api/vault/local-ca"}))
    out = hb.dispatch("homebrain.ca_info", {})
    assert out.get("ok") is True
    assert "pem" not in out
    assert "BEGIN CERTIFICATE" not in str(out)
    assert out.get("download") == "/api/vault/local-ca"


def test_nc_add_local_is_not_an_mcp_tool(hb):
    names = [t["name"] for t in hb.TOOLS]
    assert "homebrain.nc_add_local" not in names
    assert "homebrain.nc_add_local" not in hb.DISPATCH


def test_backup_schedule_set_does_not_auto_redeem(hb, monkeypatch):
    calls = []

    def capture(method, path, body=None, timeout=10):
        calls.append((method, path, body))
        return 200, {"status": "success"}

    monkeypatch.setattr(hb, "_http", capture)
    monkeypatch.setenv("HOMEBRAIN_MCP_CONSENT", "false")
    first = hb.dispatch("homebrain.backup_schedule_set", {"hour": "4", "minute": "15"})
    assert first.get("no_auto_confirm") is True
    assert calls == []
    out = hb.dispatch("homebrain.backup_schedule_set", {
        "hour": "9", "minute": "0",
        "confirmation_token": first["action_id"],
    })
    assert out.get("ok") is True
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/integrations/self/backup-schedule"
    assert calls[0][2]["hour"] == "4"
    assert calls[0][2]["minute"] == "15"


def test_household_add_strips_password_from_the_tool_result(hb, monkeypatch):
    monkeypatch.setattr(hb, "_http", lambda method, path, body=None, timeout=10: (
        200, {"user": "alex", "password": "secret", "qr": "data:x",
              "services": {"files": "ok"}}))
    first = hb.dispatch("homebrain.household_add", {"name": "Alex"})
    out = hb.dispatch("homebrain.household_add", {
        "name": "Alex", "confirmation_token": first["action_id"],
    })
    assert out.get("ok") is True
    assert "password" not in out
    assert "qr" not in out
    assert out.get("user") == "alex"


def test_watcher_set_still_auto_redeems_when_consent_is_off(monkeypatch):
    """Watchers keep today's behaviour. Config writes are the ones that must not."""
    result = {"ok": False, "requires_confirmation": True, "action_id": "abc"}
    seen = []

    def dispatch(name, args):
        seen.append(args)
        return {"ok": True}

    monkeypatch.setenv("HOMEBRAIN_MCP_CONSENT", "false")
    out = mcp_common.maybe_auto_confirm(dispatch, "homebrain.watcher_set", {}, result)
    assert out == {"ok": True}
    assert seen == [{"confirmation_token": "abc"}]
