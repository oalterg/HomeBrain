#!/usr/bin/env python3
"""Tests that a domain pasted as a URL cannot poison the derived hostnames.

The bug these pin: the wizard's "Main domain" field was filled with
`https://home.example.com` on a production disaster-restore. /start_setup
prefixed it verbatim, writing NEXTCLOUD_TRUSTED_DOMAINS=nc.https://home...
(Nextcloud: "Access through untrusted domain" on the tunnel) and
VAULT_DOMAIN=https://vault.https://home... — Vaultwarden mounts its routes
under the path it parses out of DOMAIN, so the image healthcheck's /alive
probe 404s and the container sits unhealthy forever.

Every boundary where a user-supplied domain enters .env must reduce it to a
bare hostname: /start_setup, /api/tunnel, /api/tunnel/cloudflare, and the
factory-config reverts (an already-poisoned factory file must not re-poison
.env on revert).

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_pangolin_domain_sanitize.py
    pytest scripts/tests/test_pangolin_domain_sanitize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb                                  # noqa: E402
from test_setup_credentials import Harness, _fresh  # noqa: E402

HOST = "home.example.com"


def test_sanitize_domain_reduces_urls_to_hostnames():
    cases = {
        HOST: HOST,                                # already clean
        f"https://{HOST}": HOST,                   # the production case
        f"https://{HOST}/": HOST,
        f"http://{HOST}/some/path?q=1": HOST,
        f"  {HOST}  ": HOST,                       # stray whitespace
        f"{HOST}:8443": HOST,                      # pasted with a port
        f"{HOST}.": HOST,                          # FQDN trailing dot
        "": "",
        None: "",
    }
    for raw, want in cases.items():
        got = hb.sanitize_domain(raw)
        assert got == want, f"sanitize_domain({raw!r}) = {got!r}, wanted {want!r}"


# What each derived key must contain once the domain is clean. VAULT_DOMAIN is
# the one legitimate URL — Vaultwarden wants scheme + host, nothing more.
DERIVED = {
    "PANGOLIN_DOMAIN": HOST,
    "MANAGER_DOMAIN": HOST,
    "NEXTCLOUD_TRUSTED_DOMAINS": f"nc.{HOST}",
    "HA_TRUSTED_DOMAINS": f"ha.{HOST}",
    "VAULT_TRUSTED_DOMAINS": f"vault.{HOST}",
    "VAULT_DOMAIN": f"https://vault.{HOST}",
}


def _assert_clean_writes(h):
    for key, want in DERIVED.items():
        assert h.written(key) == want, \
            f"{key} = {h.written(key)!r}, wanted {want!r}"


def test_wizard_setup_with_a_pasted_url_still_derives_clean_hostnames():
    h = _fresh()
    try:
        r = h.start(
            deployment_mode="remote",
            pangolin_id="newt-1",
            pangolin_secret="secret-1",
            pangolin_endpoint="https://pan.example",
            pangolin_domain=f"https://{HOST}/",
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        _assert_clean_writes(h)
    finally:
        h.close()


def _authed_post(h, path, payload):
    with h.client.session_transaction() as sess:
        sess["authenticated"] = True
    return h.client.post(path, json=payload)


def test_tunnel_update_with_a_pasted_url_still_derives_clean_hostnames():
    h = _fresh()
    try:
        r = _authed_post(h, "/api/tunnel", {
            "action": "update",
            "endpoint": "https://pan.example",
            "id": "newt-1",
            "secret": "secret-1",
            "main_domain": f"https://{HOST}/",
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        # /api/tunnel derives NC/HA (vault keys are deploy-owned on this path)
        for key in ("PANGOLIN_DOMAIN", "MANAGER_DOMAIN",
                    "NEXTCLOUD_TRUSTED_DOMAINS", "HA_TRUSTED_DOMAINS"):
            assert h.written(key) == DERIVED[key], \
                f"{key} = {h.written(key)!r}, wanted {DERIVED[key]!r}"
    finally:
        h.close()


def test_tunnel_revert_cleans_a_poisoned_factory_domain():
    """A factory_config written before provision.sh sanitized its --domain arg
    may still carry a URL; revert must not re-poison .env from it."""
    h = _fresh()
    try:
        hb.get_factory_config = lambda: {
            "NEWT_ID": "newt-factory",
            "NEWT_SECRET": "factory-secret",
            "PANGOLIN_ENDPOINT": "https://pan.example",
            "PANGOLIN_DOMAIN": f"https://{HOST}",
        }
        r = _authed_post(h, "/api/tunnel/revert", {})
        assert r.status_code == 200, r.get_data(as_text=True)
        _assert_clean_writes(h)
    finally:
        h.close()


def test_cloudflare_tunnel_with_a_pasted_url_stores_a_bare_hostname():
    h = _fresh()
    try:
        r = _authed_post(h, "/api/tunnel/cloudflare", {
            "domain": f"https://nc.{HOST}/",
            "service": "nc",
            "token": "cf-token",
        })
        assert r.status_code == 200, r.get_data(as_text=True)
        assert h.written("NEXTCLOUD_TRUSTED_DOMAINS") == f"nc.{HOST}"
    finally:
        h.close()


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
