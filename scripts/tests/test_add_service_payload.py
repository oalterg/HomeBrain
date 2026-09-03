#!/usr/bin/env python3
"""Adding a service later must hand back where the new account lives.

`POST /members` returns a pairing payload; `POST /members/<u>/services` used to
return only `{status, services}`. The failure is silent and total: the owner
ticks Vault for a hand-made Nextcloud account, the vault is created, and
nothing ever tells them it lives at `alex@homebrain.local` rather than `alex`.
A vault nobody can find is a vault nobody has.

The password is deliberately NOT in this payload — adding a service does not
change it, and echoing one back would invite the owner to hand over a password
HomeBrain only knows because they just typed it.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_add_service_payload.py
    pytest scripts/tests/test_add_service_payload.py
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


@contextlib.contextmanager
def _client(vault_fails=False, ha_fails=False):
    """Logged-in client with every service stubbed, all globals restored."""
    names = ["get_env_config", "_seal_password", "_nc_password_works", "nc_occ_json",
             "_create_vault_account", "_ha_token_cached", "_vault_public_url",
             "_ha_public_url"]
    saved = {n: getattr(hb, n) for n in names}
    saved["is_setup_complete"] = hb.is_setup_complete
    saved["ha_create_member"] = household.ha_create_member
    saved["TESTING"] = hb.app.config.get("TESTING")

    def _vault(uid, name, password):
        if vault_fails:
            raise household.HouseholdError("Vaultwarden said no")

    def _ha(token, name, uid, password):
        if ha_fails:
            raise household.HouseholdError("Home Assistant said no")

    hb.app.config["TESTING"] = True
    hb.is_setup_complete = lambda: True
    hb.get_env_config = lambda: dict(ENV)
    hb._seal_password = lambda uid, pw, env: None
    hb._nc_password_works = lambda uid, pw, env: True
    hb.nc_occ_json = lambda *a, **k: {"display_name": "Alex"}
    hb._create_vault_account = _vault
    hb._ha_token_cached = lambda env: "tok"
    hb._vault_public_url = lambda: "https://vault.example"
    hb._ha_public_url = lambda: "https://ha.example"
    household.ha_create_member = _ha
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        yield c
    finally:
        for n, v in saved.items():
            if n == "TESTING":
                hb.app.config["TESTING"] = v
            elif n == "ha_create_member":
                household.ha_create_member = v
            else:
                setattr(hb, n, v)


def _post(c, services, password="their-password"):
    return c.post(f"/api/household/members/{USER}/services",
                  json={"services": services, "password": password})


def test_vault_success_returns_where_to_sign_in():
    with _client() as c:
        r = _post(c, ["vault"])
    assert r.status_code == 200, r.status_code
    d = r.get_json()
    assert d["services"]["vault"] == "ok"
    assert d["vault_email"] == "alex@homebrain.local", d
    assert d["vault_url"] == "https://vault.example", d


def test_home_success_returns_its_address():
    with _client() as c:
        r = _post(c, ["home"])
    d = r.get_json()
    assert d["services"]["home"] == "ok"
    assert d["home_url"] == "https://ha.example", d


def test_no_address_for_a_service_that_failed():
    """An address for an account that was not created is worse than silence."""
    with _client(vault_fails=True) as c:
        r = _post(c, ["vault"])
    d = r.get_json()
    assert d["services"]["vault"] != "ok"
    assert "vault_url" not in d and "vault_email" not in d, d


def test_the_password_is_never_echoed_back():
    with _client() as c:
        r = _post(c, ["vault", "home"], password="s3cret-files-password")
    assert "s3cret-files-password" not in r.get_data(as_text=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all add-service payload checks passed")
