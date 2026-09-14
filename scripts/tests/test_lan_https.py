#!/usr/bin/env python3
"""LAN HTTPS is names on 443, no ports.

The product tells a phone https://nc-homebrain.local, never
http://homebrain.local:8080 or https://homebrain.local:8444. The box CA
is this box's certificate, not a Vault CA, and it is downloadable on the
LAN in both deployment modes.

    python3 -m pytest scripts/tests/test_lan_https.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


def test_local_pairing_url_is_the_nc_name():
    url, remote = hb.nc_client_url({})
    assert url == "https://nc-homebrain.local"
    assert remote is False
    assert ":" not in url.split("://", 1)[1]


def test_pairing_url_prefers_the_tunnel_when_one_exists():
    url, remote = hb.nc_client_url({"NEXTCLOUD_TRUSTED_DOMAINS": "nc.example.house"})
    assert url == "https://nc.example.house"
    assert remote is True


def test_pairing_url_does_not_print_the_old_https_port():
    url, _ = hb.nc_client_url({"NC_LOCAL_HTTPS_PORT": "8444"})
    assert "8444" not in url
    assert url == "https://nc-homebrain.local"


def test_lan_vault_and_ha_urls_are_names(monkeypatch):
    monkeypatch.setattr(hb, "is_local_mode", lambda: True)
    with hb.app.test_request_context("/", headers={"Host": "192.168.178.58"}):
        assert hb._vault_public_url() == "https://vault-homebrain.local"
        assert hb._ha_public_url() == "https://ha-homebrain.local"
        assert hb._vault_bw_url() == "https://vault-homebrain.local"


def test_remote_off_lan_keeps_the_tunnel_urls(monkeypatch):
    monkeypatch.setattr(hb, "is_local_mode", lambda: False)
    monkeypatch.setattr(hb, "get_env_config", lambda: {
        "VAULT_DOMAIN": "https://vault.example.house",
        "HA_TRUSTED_DOMAINS": "ha.example.house",
    })
    with hb.app.test_request_context("/", headers={"Host": "example.house"}):
        assert hb._vault_public_url() == "https://vault.example.house"
        assert hb._ha_public_url() == "https://ha.example.house"


def test_remote_on_lan_still_uses_local_names(monkeypatch):
    """A remote-mode box opened at homebrain.local must not print :8443."""
    monkeypatch.setattr(hb, "is_local_mode", lambda: False)
    with hb.app.test_request_context("/", headers={"Host": "homebrain.local"}):
        assert hb._vault_public_url() == "https://vault-homebrain.local"
        assert hb._ha_public_url() == "https://ha-homebrain.local"


def test_box_ca_is_served_in_remote_mode(monkeypatch):
    monkeypatch.setattr(hb, "is_local_mode", lambda: False)
    monkeypatch.setattr(hb, "compose_ps_q", lambda _s: "caddy-cid")
    monkeypatch.setattr(
        hb.subprocess, "check_output",
        lambda *a, **k: b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    )
    saved = hb.limiter.enabled
    hb.limiter.enabled = False
    hb.app.config["TESTING"] = True
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        r = c.get("/api/vault/local-ca")
        assert r.status_code == 200, r.get_data(as_text=True)
        disp = r.headers.get("Content-Disposition", "")
        assert 'filename="homebrain-ca.pem"' in disp
        assert "vault" not in disp.lower()
    finally:
        hb.limiter.enabled = saved


def test_box_ca_requires_auth(monkeypatch):
    monkeypatch.setattr(hb, "is_local_mode", lambda: True)
    saved = hb.limiter.enabled
    hb.limiter.enabled = False
    hb.app.config["TESTING"] = True
    try:
        r = hb.app.test_client().get("/api/vault/local-ca")
        assert r.status_code == 401
    finally:
        hb.limiter.enabled = saved


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
