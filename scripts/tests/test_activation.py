"""Day-2 activation predicates and the Status card wiring.

The remaining-jobs list is the product: dashboard and homebrain.setup_status
must agree. These tests do not need Nextcloud.

    python3 -m pytest scripts/tests/test_activation.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import activation            # noqa: E402
import app as hb             # noqa: E402
import integrations          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_JS = os.path.join(HERE, os.pardir, os.pardir, "src", "static", "dashboard.js")
DASHBOARD_HTML = os.path.join(HERE, os.pardir, os.pardir, "src", "templates", "dashboard.html")


def _payload(**overrides):
    base = dict(
        has_gpu=True,
        recovery_configured=True,
        wordlist_ok=True,
        telegram_paired=False,
        backup_scheduled=False,
        offsite_enabled=False,
        skips={},
        phone_members=0,
        phone_with_device=0,
    )
    base.update(overrides)
    return activation.payload(**base)


def _ids(p):
    return [s["id"] for s in p["remaining"]]


def test_fresh_gpu_box_asks_for_telegram_backup_offsite_phone():
    p = _payload()
    assert _ids(p) == ["telegram", "backup", "offsite", "phone"]
    assert p["complete"] is False
    assert p["has_gpu"] is True


def test_homecloud_never_asks_for_telegram():
    p = _payload(has_gpu=False)
    assert "telegram" not in _ids(p)
    assert _ids(p) == ["backup", "offsite", "phone"]


def test_missing_recovery_is_first_when_the_wordlist_works():
    p = _payload(recovery_configured=False)
    assert _ids(p)[0] == "recovery"


def test_broken_wordlist_does_not_block_the_card_on_an_unfixable_row():
    p = _payload(recovery_configured=False, wordlist_ok=False)
    assert "recovery" not in _ids(p)


def test_telegram_token_without_allowfrom_is_not_paired():
    assert activation.telegram_is_paired({
        "channels": {"telegram": {"enabled": True, "botToken": "x:y"}},
    }) is False


def test_telegram_allowfrom_is_paired():
    assert activation.telegram_is_paired({
        "channels": {"telegram": {"allowFrom": ["12345"]}},
    }) is True


def test_paired_drops_the_telegram_row():
    p = _payload(telegram_paired=True)
    assert "telegram" not in _ids(p)


def test_offsite_skip_drops_the_row():
    p = _payload(skips={"offsite": True})
    assert "offsite" not in _ids(p)
    assert "offsite" in p["skipped"]


def test_offsite_enabled_drops_the_row_even_without_a_skip():
    p = _payload(offsite_enabled=True)
    assert "offsite" not in _ids(p)


def test_phone_with_a_device_is_done():
    p = _payload(phone_members=1, phone_with_device=1)
    assert "phone" not in _ids(p)


def test_member_without_a_device_is_not_done():
    p = _payload(phone_members=1, phone_with_device=0)
    assert "phone" in _ids(p)


def test_phone_computers_only_skip_is_done():
    p = _payload(skips={"phone": True})
    assert "phone" not in _ids(p)


def test_restore_shaped_box_is_quiet():
    p = _payload(
        telegram_paired=True,
        backup_scheduled=True,
        offsite_enabled=True,
        phone_with_device=1,
        phone_members=1,
    )
    assert p["remaining"] == []
    assert p["complete"] is True


def test_set_skip_rejects_telegram():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "activation.json")
        ok, err = activation.set_skip("telegram", path=path)
        assert ok is False
        assert "cannot be skipped" in err


def test_set_skip_persists_offsite():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "activation.json")
        ok, err = activation.set_skip("offsite", path=path)
        assert ok is True and err == ""
        assert activation.load_skips(path)["offsite"] is True


def test_backup_snapshot_never_includes_a_password():
    snap = activation.backup_snapshot({
        "BACKUP_RETENTION": "5",
        "OFFSITE_ENABLED": "true",
        "OFFSITE_TYPE": "sftp",
        "OFFSITE_HOST": "nas.lan",
        "OFFSITE_PASS": "secret",
    })
    blob = json.dumps(snap)
    assert "secret" not in blob
    assert snap["offsite_enabled"] is True
    assert snap["offsite_host"] == "nas.lan"


def test_channel_status_exposes_paired(monkeypatch, tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({
        "channels": {"telegram": {"enabled": True, "botToken": "t",
                                  "allowFrom": ["99"]}},
        "plugins": {"entries": {"telegram": {"enabled": True}}},
    }))
    monkeypatch.setattr(integrations, "_OPENCLAW_CONFIG_PATH", str(cfg))
    info = integrations._channel_status("telegram")
    assert info["configured"] is True
    assert info["paired"] is True


def test_channel_status_bot_without_owner_is_not_paired(monkeypatch, tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({
        "channels": {"telegram": {"enabled": True, "botToken": "t"}},
        "plugins": {"entries": {"telegram": {"enabled": True}}},
    }))
    monkeypatch.setattr(integrations, "_OPENCLAW_CONFIG_PATH", str(cfg))
    info = integrations._channel_status("telegram")
    assert info["has_token"] is True
    assert info["paired"] is False


@contextmanager
def _client(gpu=True):
    saved = {
        "is_setup_complete": hb.is_setup_complete,
        "INSTALL_CREDS_PATH": hb.INSTALL_CREDS_PATH,
        "TESTING": hb.app.config.get("TESTING"),
        "has_gpu": hb.has_gpu,
        "limiter": hb.limiter.enabled,
    }
    hb.app.config["TESTING"] = True
    hb.limiter.enabled = False
    hb.is_setup_complete = lambda: True
    hb.INSTALL_CREDS_PATH = "/nonexistent"
    hb.has_gpu = lambda: gpu
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        yield c
    finally:
        hb.is_setup_complete = saved["is_setup_complete"]
        hb.INSTALL_CREDS_PATH = saved["INSTALL_CREDS_PATH"]
        hb.app.config["TESTING"] = saved["TESTING"]
        hb.has_gpu = saved["has_gpu"]
        hb.limiter.enabled = saved["limiter"]


def test_activation_route_is_session_gated():
    hb.app.config["TESTING"] = True
    c = hb.app.test_client()
    r = c.get("/api/activation")
    assert r.status_code == 401


def test_activation_route_returns_payload(monkeypatch):
    monkeypatch.setattr(hb, "activation_status", lambda: _payload(
        telegram_paired=True, backup_scheduled=True, offsite_enabled=True,
        phone_with_device=1, phone_members=1,
    ))
    with _client() as c:
        r = c.get("/api/activation")
    assert r.status_code == 200
    body = r.get_json()
    assert body["complete"] is True
    assert body["remaining"] == []


def test_skip_route_writes_the_marker(monkeypatch, tmp_path):
    path = str(tmp_path / "activation.json")
    monkeypatch.setattr(activation, "ACTIVATION_FILE", path)
    monkeypatch.setattr(hb, "activation_status", lambda: {
        "complete": False, "remaining": [], "skipped": ["offsite"],
    })
    with _client() as c:
        r = c.post("/api/activation/skip", json={"step": "offsite"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert activation.load_skips(path)["offsite"] is True


def test_skip_route_refuses_telegram():
    with _client() as c:
        r = c.post("/api/activation/skip", json={"step": "telegram"})
    assert r.status_code == 400


def test_self_activation_needs_bearer(monkeypatch):
    monkeypatch.setattr(integrations, "_self_token", lambda: "tok")
    monkeypatch.setattr(hb, "activation_status", lambda: {
        "complete": True, "remaining": [], "has_gpu": True, "skipped": [],
    })
    hb.app.config["TESTING"] = True
    hb.limiter.enabled = False
    c = hb.app.test_client()
    r = c.get("/api/integrations/self/activation")
    assert r.status_code == 401
    r = c.get("/api/integrations/self/activation",
              headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert r.get_json()["complete"] is True


def test_self_household_add_is_files_only(monkeypatch):
    seen = []

    def fake(name, user, services, pair=True):
        seen.append({"services": list(services), "pair": pair})
        return {
            "user": user, "name": name, "services": {"files": "ok"},
            "password": "secret", "qr": "data:image/svg+xml;base64,QQ",
        }, 200

    monkeypatch.setattr(hb, "create_household_member", fake)
    monkeypatch.setattr(integrations, "_self_token", lambda: "tok")
    hb.app.config["TESTING"] = True
    hb.limiter.enabled = False
    c = hb.app.test_client()
    r = c.post(
        "/api/integrations/self/household",
        json={"name": "Alex", "services": ["files", "home"]},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen == [{"services": ["files"], "pair": False}]
    body = r.get_json()
    assert body["services"] == {"files": "ok"}
    assert "password" not in body
    assert "qr" not in body
    html = open(DASHBOARD_HTML, encoding="utf-8").read()
    assert html.count('id="activation-card"') == 1
    status = html.index('<div id="status" class="tab-content')
    nxt = html.index('<div id="household" class="tab-content')
    card = html.index('id="activation-card"')
    assert status < card < nxt
    assert 'id="backup-schedule-card"' in html
    assert 'id="offsite-card"' in html
    assert 'id="recovery-prompt"' not in html


def test_init_loads_activation():
    js = open(DASHBOARD_JS, encoding="utf-8").read()
    assert "loadActivation()" in js
    boot = js[js.index("async function init()"):js.index("const POLLERS")]
    assert "loadActivation()" in boot
    assert re.search(r"\[loadActivation,\s*15000\]", js)


def test_go_to_targets_exist():
    html = open(DASHBOARD_HTML, encoding="utf-8").read()
    for card in ("recovery-card", "channels-card", "backup-schedule-card",
                 "offsite-card", "household-card"):
        assert f'id="{card}"' in html, card


def test_nuclear_reset_clears_activation_skips():
    path = os.path.join(HERE, os.pardir, "nuclear_reset.sh")
    text = open(path, encoding="utf-8").read()
    assert "activation.json" in text


def test_workspace_setup_block_keeps_secrets_off_telegram():
    path = os.path.join(HERE, os.pardir, os.pardir,
                       "config", "openclaw-workspace", "AGENTS.md")
    text = open(path, encoding="utf-8").read()
    assert "homebrain.setup_status" in text
    assert "dashboard-only" in text
    assert "household" in text.lower()
