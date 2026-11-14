#!/usr/bin/env python3
"""Remote access over the owner's private network (src/mesh.py).

What these pin, in the order the feature can lie:

  * "connected" is only ever what the daemon says, never an exit code;
  * the dashboard is never given a public address, whatever the apps get;
  * a refusal from the owner's network (HTTPS or the public address switched
    off there) leaves .env untouched — a box half-moved to an address nothing
    answers on is the failure mode docs/plans/REMOTE_ACCESS.md is written
    against;
  * the lock never hands Tailscale a way to undo it;
  * turning it off returns the box to local, the same shape Pangolin leaves.

The JSON shapes are the ones tailscale 1.104.7 prints (captured from a real
daemon, signed out). The signed-in shapes follow its documented field names
and are NOT yet confirmed on hardware — see TESTING.md.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_mesh.py
    pytest scripts/tests/test_mesh.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import app as hb                                  # noqa: E402
import mesh                                       # noqa: E402
import selftest                                   # noqa: E402
import healthcheck                                # noqa: E402
from test_setup_credentials import _fresh         # noqa: E402

NAME = "homebrain.tail1234.ts.net"
KEY = "nodekey:" + "ab" * 32
TLPUB = "tlpub:" + "cd" * 32

SIGNED_OUT = {"BackendState": "NeedsLogin", "AuthURL": "", "Self": {"DNSName": ""},
              "CurrentTailnet": None, "CertDomains": None, "Version": "1.104.7-tabc"}
RUNNING = {"BackendState": "Running", "AuthURL": "",
           "Self": {"DNSName": NAME + ".", "TailscaleIPs": ["100.64.0.1"]},
           "CurrentTailnet": {"Name": "owner@example.com"},
           "CertDomains": [NAME], "Version": "1.104.7-tabc"}


def serve_config(public):
    ports = [443, 8443, 10000, 4443]
    return {
        "TCP": {str(p): {"HTTPS": True} for p in ports},
        "Web": {f"{NAME}:{p}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}} for p in ports},
        "AllowFunnel": {f"{NAME}:{p}": True for p in (443, 8443, 10000)} if public else {},
    }


class Fake:
    """Stands in for mesh.run. Records argv; answers from a small state."""

    def __init__(self, status=RUNNING, lock=None, serve=None, refuse=None):
        self.status = status
        self.lock = lock or {"Enabled": False, "PublicKey": TLPUB, "NodeKeySigned": False}
        self.serve = serve if serve is not None else {}
        self.refuse = refuse or {}       # verb -> (rc, output)
        self.calls = []
        self._saved = mesh.run
        mesh.run = self

    def __call__(self, argv, timeout=20):
        self.calls.append(list(argv))
        if argv[:2] == ["status", "--json"]:
            return 0, json.dumps(self.status)
        if argv[:3] == ["lock", "status", "--json"]:
            return 0, json.dumps(self.lock)
        if argv[:3] == ["serve", "status", "--json"]:
            return 0, json.dumps(self.serve)
        if argv[0] in ("serve", "funnel") and "--bg" in argv:
            if argv[0] in self.refuse:
                return self.refuse[argv[0]]
            port = next(a.split("=")[1] for a in argv if a.startswith("--https="))
            self.serve.setdefault("TCP", {})[port] = {"HTTPS": True}
            funnel = self.serve.setdefault("AllowFunnel", {})
            if argv[0] == "funnel":
                funnel[f"{NAME}:{port}"] = True
            else:
                funnel.pop(f"{NAME}:{port}", None)
            return 0, "Available within your tailnet"
        if argv[:2] == ["serve", "reset"]:
            self.serve = {}
            return 0, ""
        if argv[0] == "logout":
            self.status = SIGNED_OUT
            return 0, ""
        if argv[:2] == ["lock", "init"]:
            self.lock["Enabled"] = True
            return 0, ("You are initializing tailnet lock...\n"
                       "\tdisablement-secret:AAAA1111\n\tdisablement-secret:BBBB2222\n"
                       "Initialization complete.\n")
        if argv[:2] == ["lock", "sign"]:
            return 0, ""
        raise AssertionError(f"unexpected tailscale call: {argv}")

    def close(self):
        mesh.run = self._saved

    def verbs(self):
        return [(c[0], next((a for a in c if a.startswith("--https=")), "")) for c in self.calls
                if c[0] in ("serve", "funnel") and "--bg" in c]


def _post(h, path, payload=None):
    with h.client.session_transaction() as sess:
        sess["authenticated"] = True
    return h.client.post(path, json=payload or {})


# --- module -----------------------------------------------------------------

def test_urls_put_the_dashboard_on_its_own_port():
    u = mesh.urls(NAME)
    assert u == {
        "nextcloud": f"https://{NAME}",
        "vault": f"https://{NAME}:8443",
        "homeassistant": f"https://{NAME}:10000",
        "dashboard": f"https://{NAME}:4443",
    }
    assert mesh.urls("") == {}


def test_the_dashboard_port_is_not_one_a_public_address_can_use():
    """Funnel only accepts 443, 8443 and 10000. Keeping the dashboard off all
    three is what makes "never public" structural rather than a promise."""
    assert mesh.DASHBOARD[0] not in (443, 8443, 10000)
    assert sorted(p for p, _ in mesh.APPS.values()) == [443, 8443, 10000]


def test_not_installed_is_its_own_state():
    saved = mesh.run
    mesh.run = lambda argv, timeout=20: (127, "tailscale is not installed")
    try:
        assert mesh.status() == {"state": "not_installed"}
    finally:
        mesh.run = saved


def test_signed_out_reports_no_name_even_if_one_is_cached():
    f = Fake(status=dict(SIGNED_OUT, Self={"DNSName": NAME + "."}))
    try:
        st = mesh.status()
        assert st["state"] == "needs_login"
        assert st["dns_name"] == ""
    finally:
        f.close()


def test_an_unknown_backend_state_is_never_running():
    f = Fake(status=dict(RUNNING, BackendState="SomethingNew"))
    try:
        assert mesh.status()["state"] == "stopped"
    finally:
        f.close()


def test_running_status_strips_the_trailing_dot_and_lists_waiting_devices():
    f = Fake(lock={"Enabled": True, "NodeKeySigned": True, "PublicKey": TLPUB,
                   "FilteredPeers": [{"Name": "phone.tail1234.ts.net.", "NodeKey": KEY},
                                     {"Name": "junk", "NodeKey": "not-a-key"}]},
             serve=serve_config(public=True))
    try:
        st = mesh.status()
        assert st["dns_name"] == NAME
        assert st["https_ready"] is True
        assert st["lock"] == {"enabled": True, "signed": True,
                              "pending": [{"name": "phone.tail1234.ts.net", "nodekey": KEY}]}
        assert st["public"] is True
        assert st["published"] == {"443": True, "4443": False, "8443": True, "10000": True}
    finally:
        f.close()


def test_publish_private_uses_serve_for_everything():
    f = Fake()
    try:
        assert mesh.publish(False) == {"ok": True}
        assert sorted(f.verbs()) == sorted([
            ("serve", "--https=443"), ("serve", "--https=8443"),
            ("serve", "--https=10000"), ("serve", "--https=4443")])
    finally:
        f.close()


def test_publish_public_never_funnels_the_dashboard():
    f = Fake()
    try:
        assert mesh.publish(True) == {"ok": True}
        assert ("serve", "--https=4443") in f.verbs()
        assert ("funnel", "--https=4443") not in f.verbs()
        assert sum(1 for v, _ in f.verbs() if v == "funnel") == 3
    finally:
        f.close()


def test_going_private_again_takes_the_public_address_away():
    f = Fake(serve=serve_config(public=True))
    try:
        assert mesh.publish(False) == {"ok": True}
        assert mesh.status()["public"] is False
    finally:
        f.close()


def test_publish_believes_the_daemon_not_the_exit_code():
    """Exit 0 with nothing published is the "it said it worked" bug."""
    f = Fake()
    real = f.__call__

    def lying(argv, timeout=20):
        if argv[0] in ("serve", "funnel") and "--bg" in argv:
            f.calls.append(list(argv))
            return 0, "ok"
        return real(argv, timeout)
    mesh.run = lying
    try:
        res = mesh.publish(False)
        assert "error" in res, res
    finally:
        f.close()


def test_a_refusal_carries_the_link_that_fixes_it():
    link = "https://login.tailscale.com/f/funnel?node=n123CNTRL"
    f = Fake(refuse={"funnel": (124, f"Funnel is not enabled on your tailnet.\nTo enable, visit:\n\n  {link}\n")})
    try:
        res = mesh.publish(True)
        assert res["enable_url"] == link
        assert "public address" in res["error"].lower()
    finally:
        f.close()


def test_lock_init_returns_the_secrets_and_keeps_tailscale_out():
    f = Fake()
    try:
        res = mesh.lock_init()
        assert res == {"secrets": ["disablement-secret:AAAA1111", "disablement-secret:BBBB2222"]}
        init = next(c for c in f.calls if c[:2] == ["lock", "init"])
        assert TLPUB in init and "--confirm" in init
        assert not any("support" in a for a in init), \
            "a support disablement secret lets Tailscale undo the lock"
    finally:
        f.close()


def test_lock_init_refuses_when_already_on():
    f = Fake(lock={"Enabled": True, "PublicKey": TLPUB})
    try:
        assert "error" in mesh.lock_init()
        assert not any(c[:2] == ["lock", "init"] for c in f.calls)
    finally:
        f.close()


def test_sign_rejects_anything_that_is_not_a_node_key():
    f = Fake()
    try:
        for bad in ("", "nodekey:xyz", "tlpub:" + "ab" * 32, KEY + "; rm -rf /"):
            assert "error" in mesh.lock_sign(bad)
        assert not any(c[:2] == ["lock", "sign"] for c in f.calls)
        assert mesh.lock_sign(KEY) == {"ok": True}
    finally:
        f.close()


def test_health_names_what_is_missing():
    f = Fake(serve={"TCP": {"443": {"HTTPS": True}}})
    try:
        ok, why = mesh.health()
        assert not ok and "vault" in why and "dashboard" in why
    finally:
        f.close()
    f = Fake(serve=serve_config(public=False))
    try:
        assert mesh.health()[0] is True
    finally:
        f.close()
    f = Fake(status=SIGNED_OUT)
    try:
        assert mesh.health()[0] is False
    finally:
        f.close()


def test_a_locked_out_box_is_not_healthy():
    f = Fake(lock={"Enabled": True, "NodeKeySigned": False, "PublicKey": TLPUB},
             serve=serve_config(public=False))
    try:
        ok, why = mesh.health()
        assert not ok and "locked out" in why
    finally:
        f.close()


# --- routes -----------------------------------------------------------------

def test_publish_writes_the_same_keys_a_tunnel_does():
    h = _fresh()
    f = Fake()
    try:
        r = _post(h, "/api/mesh/publish", {"public": True})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["status"] == "started"
        want = {
            "DEPLOYMENT_MODE": "remote",
            "MESH_DOMAIN": NAME,
            "NEWT_ID": "",
            "PANGOLIN_DOMAIN": "",
            "MANAGER_DOMAIN": f"{NAME}:4443",
            "NEXTCLOUD_TRUSTED_DOMAINS": NAME,
            "HA_TRUSTED_DOMAINS": f"{NAME}:10000",
            "VAULT_TRUSTED_DOMAINS": NAME,
            "VAULT_DOMAIN": f"https://{NAME}:8443",
        }
        for key, value in want.items():
            assert h.written(key) == value, f"{key} = {h.written(key)!r}, wanted {value!r}"
        assert ("CF_TOKEN_NC", None) in h.writes
        assert h.threads, "the redeploy that applies the new address never started"
    finally:
        f.close()
        h.close()


def test_a_refused_publish_writes_nothing():
    h = _fresh()
    f = Fake(refuse={"serve": (124, "Serve is not enabled on your tailnet.\n"
                                    "To enable, visit:\n  https://login.tailscale.com/f/serve?node=n1\n")})
    try:
        r = _post(h, "/api/mesh/publish", {"public": False})
        assert r.status_code == 409
        assert r.get_json()["enable_url"].startswith("https://login.tailscale.com/f/serve")
        assert h.writes == [] and not h.threads
    finally:
        f.close()
        h.close()


def test_publish_before_sign_in_is_refused():
    h = _fresh()
    f = Fake(status=SIGNED_OUT)
    try:
        r = _post(h, "/api/mesh/publish", {"public": False})
        assert r.status_code == 409
        assert h.writes == [] and not f.verbs()
    finally:
        f.close()
        h.close()


def test_switching_a_tunnel_on_clears_the_mesh_name():
    h = _fresh()
    try:
        r = _post(h, "/api/tunnel", {"endpoint": "https://p.example.com", "id": "i",
                                     "secret": "s", "main_domain": "home.example.com"})
        assert r.status_code == 200
        assert h.written("MESH_DOMAIN") == ""
    finally:
        h.close()


def test_disconnect_returns_the_box_to_local():
    h = _fresh()
    f = Fake(serve=serve_config(public=True))
    saved = hb.get_env_config
    hb.get_env_config = lambda: {"MESH_DOMAIN": NAME, "DEPLOYMENT_MODE": "remote"}
    try:
        r = _post(h, "/api/mesh/disconnect")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert ["serve", "reset"] in f.calls and ["logout"] in f.calls
        assert h.written("DEPLOYMENT_MODE") == "local"
        assert h.written("MESH_DOMAIN") == ""
        assert h.written("NEXTCLOUD_TRUSTED_DOMAINS") == ""
        assert h.written("VAULT_DOMAIN") == "https://vault-homebrain.local"
        assert h.threads
    finally:
        hb.get_env_config = saved
        f.close()
        h.close()


def test_is_local_mode_follows_the_mesh_name():
    saved = hb.get_env_config
    try:
        hb.get_env_config = lambda: {"MESH_DOMAIN": NAME, "DEPLOYMENT_MODE": "remote"}
        assert hb.is_local_mode() is False
        hb.get_env_config = lambda: {"MESH_DOMAIN": NAME, "DEPLOYMENT_MODE": "local"}
        assert hb.is_local_mode() is True
        hb.get_env_config = lambda: {"MESH_DOMAIN": ""}
        assert hb.is_local_mode() is True
    finally:
        hb.get_env_config = saved


# --- self-test and health ---------------------------------------------------

MESH_ENV = {"DEPLOYMENT_MODE": "remote", "MESH_DOMAIN": NAME}


def test_selftest_says_what_it_could_not_prove():
    saved = selftest.mesh_health
    try:
        selftest.mesh_health = lambda: (True, "Connected and published.")
        row = selftest.check_remote_access(MESH_ENV)
        assert row["status"] == selftest.OK
        assert "not tested from here" in row["detail"]
        selftest.mesh_health = lambda: (False, "The private network is not connected.")
        assert selftest.check_remote_access(MESH_ENV)["status"] == selftest.FAIL
    finally:
        selftest.mesh_health = saved


def test_healthcheck_is_crit_when_the_private_network_is_down():
    f = Fake(status=SIGNED_OUT)
    try:
        got = healthcheck.check_tunnel(MESH_ENV)
        assert got["id"] == "tunnel" and got["level"] == "crit"
    finally:
        f.close()
    f = Fake(serve=serve_config(public=False))
    try:
        assert healthcheck.check_tunnel(MESH_ENV)["level"] == "ok"
    finally:
        f.close()
    # Opted out wins, exactly as it does for a tunnel.
    assert healthcheck.check_tunnel(dict(MESH_ENV, DEPLOYMENT_MODE="local")) is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failed else 0)
