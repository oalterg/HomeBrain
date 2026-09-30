#!/usr/bin/env python3
"""An owner can add their own tunnel to a box that was set up without one.

The dashboard showed "Tunnel inactive" on a LAN-only box with no way forward;
only the setup wizard asked for Pangolin credentials. The endpoint behind the
remote-mode form was not enough on its own either: it never set
DEPLOYMENT_MODE, and the wizard had written "local", which is_local_mode()
lets win over credentials. newt would come up (get_tunnel_profiles ignores the
mode) while Nextcloud's trusted domains, the vault URL and the dashboard all
stayed local. It never wrote the vault keys, and nothing downstream derives
them on this path.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_owner_tunnel.py
    pytest scripts/tests/test_owner_tunnel.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb                                  # noqa: E402
from test_setup_credentials import _fresh  # noqa: E402

HOST = "home.example.com"
OWN_TUNNEL = {
    "endpoint": "https://pangolin.example.com",
    "id": "newt-owner",
    "secret": "owner-secret",
    "main_domain": HOST,
}


def _post(h, path, payload):
    with h.client.session_transaction() as sess:
        sess["authenticated"] = True
    return h.client.post(path, json=payload)


def test_turning_on_a_tunnel_on_a_local_box_makes_it_remote():
    h = _fresh()
    try:
        r = _post(h, "/api/tunnel", OWN_TUNNEL)
        assert r.status_code == 200, r.get_data(as_text=True)
        want = {
            "DEPLOYMENT_MODE": "remote",
            "PANGOLIN_ENDPOINT": "https://pangolin.example.com",
            "NEWT_ID": "newt-owner",
            "NEWT_SECRET": "owner-secret",
            "PANGOLIN_DOMAIN": HOST,
            "NEXTCLOUD_TRUSTED_DOMAINS": f"nc.{HOST}",
            "VAULT_TRUSTED_DOMAINS": f"vault.{HOST}",
            "VAULT_DOMAIN": f"https://vault.{HOST}",
        }
        for key, value in want.items():
            assert h.written(key) == value, f"{key} = {h.written(key)!r}, wanted {value!r}"
        assert h.written("CF_TOKEN_NC") is None and ("CF_TOKEN_NC", None) in h.writes
    finally:
        h.close()


def test_a_missing_value_is_refused_before_anything_is_written():
    """A half-filled form used to clear the Cloudflare tokens and write blanks."""
    for missing in ("endpoint", "id", "secret", "main_domain"):
        h = _fresh()
        try:
            r = _post(h, "/api/tunnel", {**OWN_TUNNEL, missing: "  "})
            assert r.status_code == 400, (missing, r.status_code)
            assert h.writes == [], (missing, h.writes)
            assert not h.threads, missing
        finally:
            h.close()


def _assert_local(h):
    want = {
        "DEPLOYMENT_MODE": "local",
        "NEWT_ID": "",
        "PANGOLIN_DOMAIN": "",
        "MANAGER_DOMAIN": "",
        "NEXTCLOUD_TRUSTED_DOMAINS": "",
        "HA_TRUSTED_DOMAINS": "",
        "VAULT_TRUSTED_DOMAINS": "",
        # Blank used to fall through to compose's http://localhost:8082.
        "VAULT_DOMAIN": "https://vault-homebrain.local",
    }
    for key, value in want.items():
        assert h.written(key) == value, f"{key} = {h.written(key)!r}, wanted {value!r}"


def test_turning_it_off_on_a_box_that_shipped_without_one_goes_local():
    h = _fresh()
    try:
        r = _post(h, "/api/tunnel", {"action": "revert"})
        assert r.status_code == 200, r.get_data(as_text=True)
        _assert_local(h)
    finally:
        h.close()


def test_dropping_cloudflare_on_a_box_that_shipped_without_a_tunnel_goes_local():
    h = _fresh()
    try:
        r = _post(h, "/api/tunnel/revert", {})
        assert r.status_code == 200, r.get_data(as_text=True)
        _assert_local(h)
    finally:
        h.close()


def test_reverting_to_a_factory_tunnel_stays_remote():
    h = _fresh()
    try:
        hb.get_factory_config = lambda: {
            "NEWT_ID": "newt-factory", "NEWT_SECRET": "factory-secret",
            "PANGOLIN_ENDPOINT": "https://pan.example", "PANGOLIN_DOMAIN": HOST,
        }
        r = _post(h, "/api/tunnel", {"action": "revert"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("DEPLOYMENT_MODE") == "remote"
        assert h.written("NEWT_ID") == "newt-factory"
        assert h.written("VAULT_DOMAIN") == f"https://vault.{HOST}"
    finally:
        h.close()


def _render(env, factory):
    saved = {k: getattr(hb, k) for k in (
        "is_setup_complete", "INSTALL_CREDS_PATH", "get_env_config", "get_factory_config")}
    saved_testing = hb.app.config.get("TESTING")
    hb.app.config["TESTING"] = True
    hb.is_setup_complete = lambda: True
    hb.INSTALL_CREDS_PATH = "/nonexistent"
    hb.get_env_config = lambda: dict(env)
    hb.get_factory_config = lambda: dict(factory)
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        r = c.get("/")
        assert r.status_code == 200, r.status_code
        html = r.get_data(as_text=True)
    finally:
        for k, v in saved.items():
            setattr(hb, k, v)
        hb.app.config["TESTING"] = saved_testing
    start = html.index('<div id="connectivity" class="tab-content')
    return html[start:html.index('<div id="settings" class="tab-content')]


LIVE = {"PANGOLIN_ENDPOINT": "https://pangolin.example.com", "NEWT_ID": "newt-owner",
        "NEWT_SECRET": "owner-secret", "PANGOLIN_DOMAIN": HOST, "DEPLOYMENT_MODE": "remote"}


def test_a_local_box_offers_the_tunnel_form():
    tab = _render({"DEPLOYMENT_MODE": "local"}, {})
    assert "Tunnel inactive" in tab
    assert "showTunnelSetup(true)" in tab
    assert 'id="pangolin-form"' in tab and 'data-enable="1"' in tab
    assert "Turn on tunnel" in tab
    assert "None" not in tab.split('id="pangolin-form"')[1].split("</form>")[0]
    assert "Switch to Cloudflare" not in tab


def test_a_remote_box_shows_the_form_without_the_enable_flag():
    tab = _render(LIVE, LIVE)
    assert "Tunnel inactive" not in tab
    assert 'id="pangolin-form"' in tab and 'data-enable="1"' not in tab
    assert "Update connection settings" in tab and "Switch to Cloudflare" in tab


def test_an_own_tunnel_on_a_box_that_shipped_without_one_is_turned_off_not_reset():
    tab = _render(LIVE, {})
    assert "Turn off tunnel" in tab and "revertPangolin(true)" in tab
    assert "Reset to factory defaults" not in tab
    factory = {**LIVE, "PANGOLIN_DOMAIN": "factory.example.com"}
    tab = _render(LIVE, factory)
    assert "Reset to factory defaults" in tab and "Turn off tunnel" not in tab


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
