"""Tests for src/household.py merge_roster and guards."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import household as hh  # noqa: E402

OWNER = "admin"
RESERVED = {"replica"}


def _nc(*uids):
    return {u: {"display_name": u.title(), "quota": "default", "last_seen": "never"}
            for u in uids}


def test_matches_across_services():
    r = hh.merge_roster(
        _nc("alex"),
        [{"email": "alex@homebrain.local", "name": "Alex"}],
        [{"username": "alex", "name": "Alex", "user_id": "u1"}],
        OWNER, reserved=RESERVED, sealed_uids={"alex"})
    assert len(r["members"]) == 1
    m = r["members"][0]
    assert m["user"] == "alex"
    assert m["files"] is True
    assert m["vault"] is True
    assert m["home"] is True
    assert m["sealed"] is True
    assert r["unmatched"] == []


def test_excludes_owner_and_reserved():
    r = hh.merge_roster(
        {**_nc("alex"), "admin": {"display_name": "Owner"}, "replica": {"display_name": "Offsite"}},
        [{"email": "admin@homebrain.local", "name": "Owner"}],
        [{"username": "admin", "name": "Owner", "is_owner": True}],
        OWNER, reserved=RESERVED)
    assert [m["user"] for m in r["members"]] == ["alex"]
    assert any(u.get("email") == "admin@homebrain.local" for u in r["unmatched"])


def test_unmatched_vault_real_email():
    r = hh.merge_roster(
        _nc("alex"),
        [{"email": "alex@gmail.com", "name": "Alex"}],
        [],
        OWNER, reserved=RESERVED)
    assert r["members"][0]["vault"] is False
    assert r["unmatched"][0]["email"] == "alex@gmail.com"


def test_mismatched_ha_username_is_two_rows():
    r = hh.merge_roster(
        _nc("alex"),
        [],
        [{"username": "Alex Smith", "name": "Alex Smith"}],
        OWNER, reserved=RESERVED)
    assert r["members"][0]["home"] is False
    assert r["unmatched"][0]["username"] == "Alex Smith"


def test_never_merges_two_people():
    r = hh.merge_roster(
        _nc("alex", "sam"),
        [{"email": "alex@homebrain.local"}, {"email": "sam@homebrain.local"}],
        [{"username": "alex"}, {"username": "sam"}],
        OWNER, reserved=RESERVED)
    assert {m["user"] for m in r["members"]} == {"alex", "sam"}
    assert all(m["vault"] and m["home"] for m in r["members"])


def test_ha_group_constant():
    assert hh.HA_GROUP == "system-users"
    assert hh.HA_GROUP != "system-admin"


def test_auth_list_parser_reads_credential_username():
    payload = {"users": [{
        "id": "abc", "name": "Alex", "group_ids": ["system-users"],
        "credentials": [{"type": "homeassistant", "data": {"username": "alex"}}],
    }]}
    users = hh.ha_usernames_from_auth_list(payload)
    assert users[0]["username"] == "alex"
    assert users[0]["user_id"] == "abc"
    assert hh.HA_GROUP in users[0]["group_ids"]


def test_ha_created_user_nested():
    uid, groups = hh.ha_created_user({
        "user": {"id": "abc123", "group_ids": ["system-users"], "name": "Alex"},
    })
    assert uid == "abc123"
    assert groups == ["system-users"]


def test_ha_created_user_flat_and_string():
    assert hh.ha_created_user({"id": "xyz", "group_ids": ["system-users"]})[0] == "xyz"
    assert hh.ha_created_user("raw-id")[0] == "raw-id"


def test_empty_inputs():
    r = hh.merge_roster({}, [], [], OWNER, reserved=RESERVED)
    assert r["members"] == []
    assert r["unmatched"] == []
