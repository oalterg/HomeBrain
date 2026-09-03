#!/usr/bin/env python3
"""An id free in Nextcloud can still be taken in the vault or in Home Assistant.

`merge_roster` joins on the Nextcloud uid, so creating Files `alex` while a
vault or HA `alex` exists does not fail — it merges the two into one roster row
standing for two different passwords, with the sealed blob describing only the
newer one. Nothing says a word, and `POST /members` used to check Nextcloud
alone.

The dangerous case is the one where the service was NOT ticked: ticking Home
Assistant at least surfaces a per-service error from `ha_create_member`, while
leaving it unticked creates the broken row in silence.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_member_id_collision.py
    pytest scripts/tests/test_member_id_collision.py
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import household           # noqa: E402

USER = "alex"
ENV = {"RECOVERY_BACKUP_KEY": "k" * 32, "HA_PASSWORD_MANAGED": "true",
       "NEXTCLOUD_ADMIN_USER": "admin"}
BOOM = RuntimeError("Vaultwarden is restarting")


@contextlib.contextmanager
def _client(vault_users=(), ha_users=(), vault_raises=False, ha_raises=False):
    names = ["get_env_config", "nc_occ_json", "nc_occ", "_list_vault_users",
             "_list_ha_users", "_seal_password", "ensure_default_quota",
             "ensure_photo_settings", "pairing_payload", "_vault_public_url",
             "_ha_public_url"]
    saved = {n: getattr(hb, n) for n in names}
    saved["is_setup_complete"] = hb.is_setup_complete
    saved["TESTING"] = hb.app.config.get("TESTING")

    created = []

    def _occ(*args, **kwargs):
        created.append(args)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _vault_list():
        if vault_raises:
            raise BOOM
        return list(vault_users)

    def _ha_list(env):
        if ha_raises:
            raise BOOM
        return list(ha_users)

    hb.app.config["TESTING"] = True
    hb.is_setup_complete = lambda: True
    hb.get_env_config = lambda: dict(ENV)
    hb.nc_occ_json = lambda *a, **k: {}          # Nextcloud has nobody
    hb.nc_occ = _occ
    hb._list_vault_users = _vault_list
    hb._list_ha_users = _ha_list
    hb._seal_password = lambda uid, pw, env: None
    hb.ensure_default_quota = lambda: None
    hb.ensure_photo_settings = lambda: None
    hb.pairing_payload = lambda user, pw, env: {"user": user, "url": "u", "qr": "q"}
    hb._vault_public_url = lambda: "https://vault.example"
    hb._ha_public_url = lambda: "https://ha.example"
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        c.created = created
        yield c
    finally:
        for n, v in saved.items():
            if n == "TESTING":
                hb.app.config["TESTING"] = v
            else:
                setattr(hb, n, v)


def _add(c, services=("files",)):
    return c.post("/api/household/members",
                  json={"name": "Alex", "user": USER, "services": list(services)})


def test_ha_login_blocks_a_files_only_add():
    """The silent case: HA was never ticked, so nothing else would complain."""
    with _client(ha_users=[{"username": USER, "name": "Alex"}]) as c:
        r = _add(c)
        assert r.status_code == 400, r.status_code
        assert "Home Assistant" in r.get_json()["error"]
        assert c.created == [], "created the Nextcloud user anyway"


def test_vault_account_blocks_a_files_only_add():
    with _client(vault_users=[{"email": "alex@homebrain.local"}]) as c:
        r = _add(c)
        assert r.status_code == 400, r.status_code
        assert "Vault" in r.get_json()["error"]
        assert c.created == []


def test_a_free_id_still_goes_through():
    with _client(vault_users=[{"email": "sam@homebrain.local"}],
                 ha_users=[{"username": "sam"}]) as c:
        r = _add(c)
        assert r.status_code == 200, r.get_json()
        assert any("user:add" in a for a in c.created), c.created


def test_unmanaged_home_assistant_is_not_consulted():
    """Its accounts never enter the roster, so none of them can merge."""
    env = dict(ENV, HA_PASSWORD_MANAGED="false")
    with _client(ha_users=[{"username": USER}]) as c:
        hb.get_env_config = lambda: dict(env)
        r = _add(c)
    assert r.status_code == 200, r.get_json()


def test_a_service_that_cannot_be_asked_blocks_rather_than_guesses():
    """Silence is not evidence of absence — and the bad outcome is unrecoverable."""
    with _client(vault_raises=True) as c:
        r = _add(c)
        assert r.status_code == 502, r.status_code
        assert "Try again" in r.get_json()["error"]
        assert c.created == []
    with _client(ha_raises=True) as c:
        r = _add(c)
        assert r.status_code == 502, r.status_code
        assert c.created == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all collision checks passed")
