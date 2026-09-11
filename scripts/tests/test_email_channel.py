"""Agent mailbox flag, From≠To channel gate, session-only toggle.

Run: python3 -m pytest scripts/tests/test_email_channel.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import integrations         # noqa: E402


def test_normalize_email_parseaddr():
    assert integrations.normalize_email('Name <You@Example.COM>') == "you@example.com"
    assert integrations.normalize_email("you@example.com") == "you@example.com"
    assert integrations.normalize_email("") == ""


def test_migrate_one_account_missing_key_becomes_true():
    out, changed = integrations.migrate_email_agent_mailbox(
        [{"name": "Personal", "user": "a@b.c"}])
    assert changed is True
    assert out[0]["agent_mailbox"] is True


def test_migrate_two_accounts_missing_key_stay_false():
    out, changed = integrations.migrate_email_agent_mailbox([
        {"name": "A", "user": "a@b.c"},
        {"name": "B", "user": "b@b.c"},
    ])
    assert changed is True
    assert out[0]["agent_mailbox"] is False
    assert out[1]["agent_mailbox"] is False


def test_migrate_present_key_unchanged():
    src = [{"name": "A", "user": "a@b.c", "agent_mailbox": True}]
    out, changed = integrations.migrate_email_agent_mailbox(src)
    assert changed is False
    assert out[0]["agent_mailbox"] is True


def test_from_to_ok_requires_outside_from():
    accounts = [
        {"name": "Agent", "user": "agent@box.test", "agent_mailbox": True},
    ]
    assert integrations.email_channel_from_to_ok(["owner@box.test"], accounts)
    assert not integrations.email_channel_from_to_ok(["agent@box.test"], accounts)
    assert not integrations.email_channel_from_to_ok([], accounts)


@pytest.fixture
def email_box(tmp_path, monkeypatch):
    monkeypatch.setattr(integrations, "EMAIL_ACCOUNTS_FILE",
                        str(tmp_path / "email_accounts.json"))
    monkeypatch.setattr(integrations, "EMAIL_CHANNEL_FILE",
                        str(tmp_path / "email_channel.json"))
    monkeypatch.setattr(integrations, "_encrypt_secret", lambda p: p)
    monkeypatch.setattr(integrations, "_read_env",
                        lambda: {"CLOUD_EMAIL": "owner@example.com"})
    monkeypatch.setattr(integrations, "reconcile_one", lambda *a, **k: None)
    monkeypatch.setattr(integrations, "_openclaw_daemon_restart", lambda: None)
    monkeypatch.setattr(hb.limiter, "enabled", False)
    hb.app.config["TESTING"] = True
    client = hb.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return tmp_path, client


def _add(name="Agent", user="agent@box.test", agent_mailbox=None):
    return integrations.add_email_account(
        name, user, "imap.example", 993, "smtp.example", 587, "pw",
        agent_mailbox=agent_mailbox)


def test_add_first_defaults_agent_mailbox(email_box):
    ok, _ = _add()
    assert ok
    acc = integrations._load_email_accounts()
    assert acc[0]["agent_mailbox"] is True


def test_add_second_defaults_owner_inbox(email_box):
    _add()
    ok, _ = _add("Personal", "oliver@example.com")
    assert ok
    acc = {a["name"]: a for a in integrations._load_email_accounts()}
    assert acc["Agent"]["agent_mailbox"] is True
    assert acc["Personal"]["agent_mailbox"] is False


def test_add_body_overrides_default(email_box):
    ok, _ = _add(agent_mailbox=False)
    assert ok
    assert integrations._load_email_accounts()[0]["agent_mailbox"] is False


def test_agent_mailbox_requires_session(email_box):
    _add()
    hb.app.config["TESTING"] = True
    anon = hb.app.test_client()
    res = anon.post("/api/integrations/email/agent-mailbox",
                    json={"name": "Agent", "agent_mailbox": False})
    assert res.status_code == 401


def test_agent_mailbox_toggle_session(email_box):
    _add()
    _, client = email_box
    res = client.post("/api/integrations/email/agent-mailbox",
                      json={"name": "Agent", "agent_mailbox": False})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert integrations._load_email_accounts()[0]["agent_mailbox"] is False


def test_channel_enable_400_without_agent_mailbox(email_box):
    _, client = email_box
    res = client.post("/api/channels/email",
                      json={"enabled": True, "allow_from": ["owner@example.com"]})
    assert res.status_code == 400


def test_channel_enable_400_when_from_equals_to(email_box):
    _add("Agent", "agent@box.test", True)
    _, client = email_box
    res = client.post("/api/channels/email",
                      json={"enabled": True, "allow_from": ["agent@box.test"]})
    assert res.status_code == 400
    assert b"From" in res.data or b"from" in res.data.lower()


def test_channel_enable_200_distinct_from(email_box):
    _add("Agent", "agent@box.test", True)
    _, client = email_box
    res = client.post("/api/channels/email",
                      json={"enabled": True, "allow_from": ["owner@example.com"]})
    assert res.status_code == 200, res.get_data(as_text=True)
    d = res.get_json()
    assert d["enabled"] is True
    assert d.get("ready") is True
    assert d["key"] == "email"
    ch = json.loads((email_box[0] / "email_channel.json").read_text())
    assert ch["enabled"] is True
    assert ch["allow_from"] == ["owner@example.com"]


def test_channels_status_composes_email(email_box):
    _, client = email_box
    res = client.get("/api/channels/status")
    assert res.status_code == 200
    keys = [c["key"] for c in res.get_json()["channels"]]
    assert "telegram" in keys
    assert "email" in keys


def test_load_migrates_and_writes(email_box):
    path = email_box[0] / "email_accounts.json"
    path.write_text(json.dumps({"accounts": [{"name": "Old", "user": "a@b.c"}]}))
    acc = integrations._load_email_accounts()
    assert acc[0]["agent_mailbox"] is True
    on_disk = json.loads(path.read_text())
    assert on_disk["accounts"][0]["agent_mailbox"] is True
