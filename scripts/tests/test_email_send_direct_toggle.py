"""Email send-direct toggle must not rewrite .env.

The bug: email_send_direct_toggle read every key via _read_env (which
strips quotes), flipped one flag, and dumped `k=v` unquoted. That turned
VAULT_ADMIN_TOKEN='$argon2id$…' into VAULT_ADMIN_TOKEN=$argon2id$…, which
`source` under set -u aborts on — /api/system/config 500s, dashboard says
the AI agent is not installed.

The toggle must go through update_env_var (one key) and leave the PHC line
untouched.

Runnable: python3 -m pytest scripts/tests/test_email_send_direct_toggle.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import integrations         # noqa: E402

PHC = "$argon2id$v=19$m=65540$t=3$p=4$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNo"
PARAMS = "scrypt$n=32768$r=8$p=1$dklen=32"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        f"VAULT_ADMIN_TOKEN='{PHC}'\n"
        f"RECOVERY_PARAMS='{PARAMS}'\n"
        "HOMEBRAIN_EMAIL_SEND_DIRECT=false\n"
        "MASTER_PASSWORD=secret\n"
    )
    monkeypatch.setattr(hb, "ENV_FILE", str(path))
    monkeypatch.setattr(hb.limiter, "enabled", False)
    monkeypatch.setattr(integrations, "_openclaw_daemon_restart", lambda: None)
    hb.app.config["TESTING"] = True
    client = hb.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return path, client


def test_toggle_calls_update_env_var_for_one_key(env_file, monkeypatch):
    path, client = env_file
    calls = []

    def fake_update(k, v):
        calls.append((k, v))
        return True

    monkeypatch.setattr(hb, "update_env_var", fake_update)
    res = client.post("/api/integrations/email/send-direct-toggle",
                      json={"enabled": True})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == [("HOMEBRAIN_EMAIL_SEND_DIRECT", "true")]
    text = path.read_text()
    assert f"VAULT_ADMIN_TOKEN='{PHC}'" in text
    assert f"RECOVERY_PARAMS='{PARAMS}'" in text


def test_toggle_does_not_unquote_phc_when_update_fails_closed(env_file, monkeypatch):
    path, client = env_file
    monkeypatch.setattr(hb, "update_env_var", lambda *a, **k: False)
    res = client.post("/api/integrations/email/send-direct-toggle",
                      json={"enabled": True})
    assert res.status_code == 500
    text = path.read_text()
    assert f"VAULT_ADMIN_TOKEN='{PHC}'" in text
    assert "HOMEBRAIN_EMAIL_SEND_DIRECT=false" in text
