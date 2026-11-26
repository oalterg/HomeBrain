#!/usr/bin/env python3
"""sanitize_domain reduces a domain to a bare hostname.

The bug this pins: a domain pasted as `https://home.example.com` on a
production disaster-restore was prefixed verbatim, writing
NEXTCLOUD_TRUSTED_DOMAINS=nc.https://home... (Nextcloud: "Access through
untrusted domain") and VAULT_DOMAIN=https://vault.https://home... —
Vaultwarden mounts its routes under the path it parses out of DOMAIN, so the
image healthcheck's /alive probe 404s and the container sits unhealthy
forever. The private-network name goes through it too (point_mesh_at), and a
DNS name can carry a trailing dot.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_domain_sanitize.py
    pytest scripts/tests/test_domain_sanitize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb                                  # noqa: E402

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
