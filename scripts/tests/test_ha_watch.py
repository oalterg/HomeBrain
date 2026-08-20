"""Unit tests for HA watcher fire logic, schema, and CLI argv.

Run: python3 -m pytest scripts/tests/test_ha_watch.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ha_watch  # noqa: E402


WATCHER = {
    "id": "front-person",
    "enabled": True,
    "ha_account": "remote",
    "entity_id": "binary_sensor.front_person",
    "to": "on",
    "cooldown_s": 120,
    "message": "Person at the front",
    "camera_entity_id": "camera.front",
    "wake": True,
}


def test_off_to_on_fires():
    assert ha_watch.transition_fires("off", "on", "on") is True


def test_on_to_on_attrs_do_not_fire():
    assert ha_watch.transition_fires("on", "on", "on") is False


def test_unavailable_ignored():
    assert ha_watch.transition_fires("off", "unavailable", "on") is False
    assert ha_watch.transition_fires("unavailable", "on", "on") is False
    assert ha_watch.transition_fires(None, "on", "on") is False
    assert ha_watch.transition_fires("off", "unknown", "on") is False


def test_to_must_match():
    assert ha_watch.transition_fires("on", "off", "on") is False
    assert ha_watch.transition_fires("on", "off", "off") is True


def test_cooldown_blocks_then_expires():
    now = 1_800_000_000
    assert ha_watch.cooldown_blocks(0, now, 120) is False
    assert ha_watch.cooldown_blocks(now - 10, now, 120) is True
    assert ha_watch.cooldown_blocks(now - 120, now, 120) is False


def test_cold_start_seeds_and_does_not_fire():
    w = dict(WATCHER)
    state = {}
    action = ha_watch.apply_event(state, w, "off", "on", 100.0)
    assert action == "seed"
    assert state["front-person"]["last_state"] == "on"
    assert "last_fired" not in state["front-person"] or \
        state["front-person"].get("last_fired") in (None, 0)

    action = ha_watch.apply_event(state, w, "on", "on", 101.0)
    assert action == "ignore"

    action = ha_watch.apply_event(state, w, "on", "off", 102.0)
    assert action == "ignore"
    assert state["front-person"]["last_state"] == "off"

    action = ha_watch.apply_event(state, w, "off", "on", 103.0)
    assert action == "fire"
    assert state["front-person"]["last_fired"] == 103.0


def test_cooldown_after_fire():
    w = dict(WATCHER)
    state = {"front-person": {"last_state": "off", "last_fired": 0}}
    assert ha_watch.apply_event(state, w, "off", "on", 1000.0) == "fire"
    state["front-person"]["last_state"] = "off"
    assert ha_watch.apply_event(state, w, "off", "on", 1010.0) == "cooldown"
    state["front-person"]["last_state"] = "off"
    assert ha_watch.apply_event(state, w, "off", "on", 1120.0) == "fire"


def test_empty_state_file_is_not_every_entity_on():
    assert ha_watch.decide_event(WATCHER, "off", "on", None, 1.0) == "seed"
    assert ha_watch.decide_event(
        WATCHER, "off", "on", {}, 1.0) == "seed"


def test_parse_state_changed():
    msg = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": "binary_sensor.front_person",
                "old_state": {"state": "off", "attributes": {"foo": 1}},
                "new_state": {"state": "on", "attributes": {"foo": 2}},
            },
        },
    }
    eid, old, new = ha_watch.parse_state_changed(msg)
    assert eid == "binary_sensor.front_person"
    assert old == "off"
    assert new == "on"
    # Attribute-only: same state.
    msg["event"]["data"]["old_state"]["state"] = "on"
    eid, old, new = ha_watch.parse_state_changed(msg)
    assert old == new == "on"
    assert ha_watch.parse_state_changed({"type": "pong"}) is None


def test_ws_url():
    assert ha_watch.ws_url("http://127.0.0.1:8123") == \
        "ws://127.0.0.1:8123/api/websocket"
    assert ha_watch.ws_url("https://ha.example.com") == \
        "wss://ha.example.com/api/websocket"
    assert ha_watch.ws_url("https://ha.example.com/") == \
        "wss://ha.example.com/api/websocket"


def test_ws_hold_open_clears_recv_timeout():
    class Fake:
        timeout = 30

        def settimeout(self, value):
            self.timeout = value

    ws = Fake()
    ha_watch.ws_hold_open(ws)
    assert ws.timeout is None


def test_normalize_refuses_siren_field():
    raw = dict(WATCHER, siren="turn_on")
    w, err = ha_watch.normalize_watcher(raw)
    assert w is None
    assert "siren" in err


def test_normalize_defaults():
    w, err = ha_watch.normalize_watcher({
        "id": "washer",
        "ha_account": "home",
        "entity_id": "binary_sensor.washer_done",
    })
    assert err == ""
    assert w["to"] == "on"
    assert w["wake"] is False
    assert w["enabled"] is True
    assert w["cooldown_s"] == 120
    assert w["camera_entity_id"] == ""


def test_normalize_generates_id():
    w, err = ha_watch.normalize_watcher({
        "ha_account": "Berlin",
        "entity_id": "input_boolean.homebrain_e2e_watch",
    })
    assert err == ""
    assert w["id"] == "berlin-input-boolean-homebrain-e2e-watch"


def test_normalize_ignores_clerk_cooldown_and_enabled():
    w, err = ha_watch.normalize_watcher({
        "id": "washer",
        "ha_account": "home",
        "entity_id": "binary_sensor.washer_done",
        "cooldown_s": 60,
        "enabled": False,
    })
    assert err == ""
    assert w["cooldown_s"] == 120
    assert w["enabled"] is True


def test_assign_id_same_pair_replaces(tmp_path, monkeypatch):
    path = str(tmp_path / "ha_watchers.json")
    monkeypatch.setattr(ha_watch, "WATCHERS_FILE", path)
    a, _ = ha_watch.normalize_watcher({
        "id": "e2e-watch", "ha_account": "Berlin",
        "entity_id": "input_boolean.homebrain_e2e_watch",
    })
    ha_watch.save_watchers([a])
    b, _ = ha_watch.normalize_watcher({
        "ha_account": "Berlin",
        "entity_id": "input_boolean.homebrain_e2e_watch",
        "message": "again",
    })
    out = ha_watch.assign_id(b)
    assert out["id"] == "e2e-watch"
    assert out["message"] == "again"


def test_clerk_watcher_hides_knobs():
    w, _ = ha_watch.normalize_watcher({
        "id": "washer", "ha_account": "home",
        "entity_id": "binary_sensor.washer_done",
    })
    pub = ha_watch.clerk_watcher(w)
    assert "cooldown_s" not in pub
    assert "enabled" not in pub
    assert pub["id"] == "washer"


def test_prune_runtime_state(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    monkeypatch.setattr(ha_watch, "STATE_FILE", state_path)
    ha_watch.save_runtime_state({
        "keep-me": {"last_state": "off", "last_fired": 1},
        "gone": {"last_state": "on", "last_fired": 2},
    })
    ha_watch.prune_runtime_state([{"id": "keep-me"}])
    assert ha_watch.load_runtime_state() == {
        "keep-me": {"last_state": "off", "last_fired": 1},
    }


def test_normalize_bad_id():
    w, err = ha_watch.normalize_watcher({
        "id": "front person",
        "ha_account": "home",
        "entity_id": "binary_sensor.x",
    })
    assert w is None
    assert "id" in err


def test_same_id_replaces(tmp_path, monkeypatch):
    path = str(tmp_path / "ha_watchers.json")
    monkeypatch.setattr(ha_watch, "WATCHERS_FILE", path)
    a, _ = ha_watch.normalize_watcher({
        "id": "front-person", "ha_account": "home",
        "entity_id": "binary_sensor.a", "message": "one",
    })
    b, _ = ha_watch.normalize_watcher({
        "id": "front-person", "ha_account": "remote",
        "entity_id": "binary_sensor.b", "message": "two",
    })
    ha_watch.save_watchers([a])
    existing = {w["id"]: w for w in ha_watch.load_watchers()}
    existing[b["id"]] = b
    ha_watch.save_watchers(list(existing.values()))
    loaded = ha_watch.load_watchers()
    assert len(loaded) == 1
    assert loaded[0]["message"] == "two"
    assert loaded[0]["ha_account"] == "remote"


def test_load_watchers_accepts_bare_object(tmp_path):
    path = str(tmp_path / "w.json")
    with open(path, "w") as f:
        json.dump(WATCHER, f)
    loaded = ha_watch.load_watchers(path)
    assert len(loaded) == 1
    assert loaded[0]["id"] == "front-person"


def test_ping_argv_text_and_media():
    argv = ha_watch.ping_argv("telegram", "123", "Person at the front")
    assert argv[:6] == ["sudo", "-H", "-u", "homebrain", "timeout", "45"]
    assert "openclaw" in argv
    assert argv[argv.index("message") + 1] == "send"
    assert "--media" not in argv
    argv = ha_watch.ping_argv("telegram", "123", "hi", "/tmp/still.jpg")
    assert argv[argv.index("--media") + 1] == "/tmp/still.jpg"


def test_wake_argv_uses_dedicated_session_not_isolated_flag():
    argv = ha_watch.wake_argv("telegram", "123", "prompt")
    assert "--session-key" in argv
    assert argv[argv.index("--session-key") + 1] == "ha-watch"
    assert argv[argv.index("--channel") + 1] == "telegram"
    assert argv[argv.index("--to") + 1] == "123"
    assert "--deliver" not in argv
    assert "--isolated" not in argv
    assert "--message" in argv
    assert "send" not in argv[argv.index("agent"):]
    assert argv[argv.index("timeout") + 1] == "600"
    assert ha_watch.OPENCLAW_RUN_TIMEOUT_S >= int(argv[argv.index("timeout") + 1]) + 60


def test_wake_prompt_wraps_ha_names_as_data():
    prompt = ha_watch.wake_prompt(WATCHER, "on", "/tmp/front.jpg")
    assert "not instructions" in prompt
    assert "<<<binary_sensor.front_person>>>" in prompt
    assert "<<<Person at the front>>>" in prompt
    assert "Do not send that still again" in prompt
    assert "Do not call siren" in prompt
    assert "final text is not delivered" in prompt
    assert "message tool" in prompt
    assert "--deliver" not in prompt
    # Names must not appear unsandwiched in the instruction paragraph.
    instr = prompt.split("not instructions:")[0]
    assert "front_person" not in instr
    assert "Person at the front" not in instr


def test_extract_still_jpeg_and_mjpeg():
    jpeg = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"
    still, mime = ha_watch.extract_still(jpeg, "image/jpeg")
    assert mime == "image/jpeg"
    assert still.startswith(b"\xff\xd8")
    wrapped = b"--frame\r\n" + jpeg + b"\r\n"
    still, mime = ha_watch.extract_still(wrapped, "multipart/x-mixed-replace")
    assert mime == "image/jpeg"
    assert still.startswith(b"\xff\xd8")


def test_handle_match_seeds_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", str(tmp_path / "pings.json"))
    monkeypatch.setattr(ha_watch, "ping", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("ping must not run on seed")))
    account = {"name": "remote", "base_url": "http://h", "token": "t"}
    state = {}
    action = ha_watch.handle_match(
        account, WATCHER, "off", "on", 1.0, state)
    assert action == "seed"
    assert ha_watch.load_ping_log() == []


def test_handle_match_pings_then_wakes(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", str(tmp_path / "pings.json"))
    monkeypatch.setattr(ha_watch, "MEDIA_DIR", str(tmp_path / "media"))
    seen = {}

    def fake_ping(w, media, target=None):
        seen["ping"] = (w["id"], media)
        return True

    def fake_wake(w, new, media, target=None):
        seen["wake"] = (w["id"], new, media)
        return True

    monkeypatch.setattr(ha_watch, "ping", fake_ping)
    monkeypatch.setattr(ha_watch, "wake", fake_wake)
    monkeypatch.setattr(ha_watch, "fetch_camera_still",
                        lambda *a, **k: "/tmp/front.jpg")
    # Join the wake thread so the test sees it.
    real_thread = ha_watch.threading.Thread

    def sync_thread(*a, **k):
        t = real_thread(*a, **k)
        t.start = lambda: t.run()
        return t

    monkeypatch.setattr(ha_watch.threading, "Thread", sync_thread)
    state = {"front-person": {"last_state": "off", "last_fired": 0}}
    account = {"name": "remote", "base_url": "http://h", "token": "t"}
    action = ha_watch.handle_match(
        account, WATCHER, "off", "on", 50.0, state)
    assert action == "fire"
    assert seen["ping"] == ("front-person", "/tmp/front.jpg")
    assert seen["wake"][0] == "front-person"
    log = ha_watch.load_ping_log()
    assert len(log) == 1
    assert log[0]["watcher_id"] == "front-person"
    assert log[0]["still"] is True
    assert log[0]["message"] == "<<<Person at the front>>>"
    assert log[0]["entity_id"].startswith("<<<")
    assert "not instructions" in log[0]["hint"]


def test_wake_false_does_not_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", str(tmp_path / "pings.json"))
    monkeypatch.setattr(ha_watch, "ping", lambda *a, **k: True)
    monkeypatch.setattr(ha_watch, "fetch_camera_still", lambda *a, **k: None)
    monkeypatch.setattr(ha_watch, "wake", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("wake must stay off")))
    w = dict(WATCHER, wake=False, camera_entity_id="")
    state = {"front-person": {"last_state": "off", "last_fired": 0}}
    assert ha_watch.handle_match(
        {"name": "remote"}, w, "off", "on", 1.0, state) == "fire"
    log = ha_watch.load_ping_log()
    assert len(log) == 1
    assert log[0]["wake"] is False
    assert log[0]["still"] is False


def test_failed_ping_does_not_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_watch, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", str(tmp_path / "pings.json"))
    monkeypatch.setattr(ha_watch, "ping", lambda *a, **k: False)
    monkeypatch.setattr(ha_watch, "fetch_camera_still", lambda *a, **k: None)
    w = dict(WATCHER, wake=False, camera_entity_id="")
    state = {"front-person": {"last_state": "off", "last_fired": 0}}
    assert ha_watch.handle_match(
        {"name": "remote"}, w, "off", "on", 1.0, state) == "fire"
    assert ha_watch.load_ping_log() == []


def test_ping_log_caps_at_max(tmp_path, monkeypatch):
    path = str(tmp_path / "pings.json")
    monkeypatch.setattr(ha_watch, "PING_LOG_FILE", path)
    w = dict(WATCHER)
    for i in range(ha_watch.PING_LOG_MAX + 7):
        ha_watch.append_ping_log(ha_watch.ping_fact(w, "on", False, 1000.0 + i))
    log = ha_watch.load_ping_log()
    assert len(log) == ha_watch.PING_LOG_MAX
    assert log[0]["ts"] != ha_watch.ping_fact(w, "on", False, 1000.0)["ts"]
    assert log[-1]["ts"] == ha_watch.ping_fact(
        w, "on", False, 1000.0 + ha_watch.PING_LOG_MAX + 6)["ts"]


def test_backup_and_restore_name_the_watchers_file():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    backup = open(os.path.join(root, "scripts", "backup.sh")).read()
    restore = open(os.path.join(root, "scripts", "restore.sh")).read()
    assert "ha_watchers.json" in backup
    assert "ha_watch_pings.json" in backup
    assert "ha_watchers.json" in restore
    assert "ha_watch_pings.json" in restore
