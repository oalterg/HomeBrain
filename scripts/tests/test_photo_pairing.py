#!/usr/bin/env python3
"""Tests for phone pairing — the account a QR code is allowed to carry.

The property worth pinning hardest: **a QR code never carries the admin
account.** A Nextcloud app password holds its account's full rights and is not
scoped to files, so an admin one put behind a QR code held up to a phone camera
is an admin credential for Nextcloud and everything the Passwords app holds,
usable from anywhere the box is reachable.

`pairing_payload` is the single funnel every pairing route goes through. The
guard lives there rather than in each route so a route added later cannot
forget it — that is the behaviour these tests exist to keep true.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_photo_pairing.py
    pytest scripts/tests/test_photo_pairing.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

ADMIN = "admin"
MEMBER = "alex"
TOKEN = "AbCdE-fGhIj-KlMnO-pQrSt-UvWxY"      # occ app-password shape, >20 chars
ENV = {
    "NEXTCLOUD_ADMIN_USER": ADMIN,
    "NEXTCLOUD_TRUSTED_DOMAINS": "nc.example.house",
}


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def occ_calls(monkeypatch):
    """Record every occ invocation; mint a plausible token for app-password."""
    calls = []

    def fake_occ(*args, **kwargs):
        calls.append(args)
        if args and args[0] == "user:add-app-password":
            return FakeProc(stdout=f"The password is: {TOKEN}\n")
        return FakeProc()

    monkeypatch.setattr(hb, "nc_occ", fake_occ)
    return calls


@pytest.fixture
def no_qrencode(monkeypatch):
    """qrencode is a subprocess; this file must not shell out."""
    monkeypatch.setattr(hb.subprocess, "run",
                        lambda *a, **k: FakeProc(stdout="<svg/>"))


# --- the invariant ----------------------------------------------------------

def test_admin_account_is_never_paired(occ_calls, no_qrencode):
    with pytest.raises(hb.NextcloudError):
        hb.pairing_payload(ADMIN, "pw", ENV)


def test_refusing_admin_mints_nothing(occ_calls, no_qrencode):
    """The refusal must come before the token exists. A guard that rejects the
    response after occ has already created an app password has still created
    an admin credential."""
    with pytest.raises(hb.NextcloudError):
        hb.pairing_payload(ADMIN, "pw", ENV)
    assert occ_calls == [], f"occ was called while refusing: {occ_calls}"


def test_admin_is_matched_by_configured_name_not_the_literal(occ_calls, no_qrencode):
    """A box whose admin account is not called "admin" is just as protected."""
    env = dict(ENV, NEXTCLOUD_ADMIN_USER="owner")
    with pytest.raises(hb.NextcloudError):
        hb.pairing_payload("owner", "pw", env)
    assert occ_calls == []


def test_empty_admin_setting_does_not_block_everyone(occ_calls, no_qrencode):
    """If NEXTCLOUD_ADMIN_USER is missing, the guard must not match every user
    with a falsy comparison and lock the household out of pairing."""
    payload = hb.pairing_payload(MEMBER, "pw", {"NEXTCLOUD_TRUSTED_DOMAINS": "nc.example.house"})
    assert payload["user"] == MEMBER


def test_no_route_mints_for_admin():
    """The admin-only pairing route is gone, not merely unlinked from the UI."""
    rules = {r.rule for r in hb.app.url_map.iter_rules()}
    assert "/api/photos/pair" not in rules


# --- the path that must still work -----------------------------------------

def test_member_pairing_still_returns_a_scannable_payload(occ_calls, no_qrencode):
    payload = hb.pairing_payload(MEMBER, "pw", ENV)
    assert payload["user"] == MEMBER
    assert payload["url"] == "https://nc.example.house"
    assert payload["remote"] is True
    assert payload["qr"].startswith("data:image/svg+xml;base64,")
    assert ("user:add-app-password", MEMBER, "--password-from-env") in occ_calls


def test_token_never_reaches_the_process_table(monkeypatch, occ_calls):
    """The app password goes to qrencode on stdin, not argv."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input", "")
        return FakeProc(stdout="<svg/>")

    monkeypatch.setattr(hb.subprocess, "run", fake_run)
    hb.pairing_payload(MEMBER, "pw", ENV)
    assert TOKEN not in " ".join(seen["cmd"])
    assert TOKEN in seen["input"]


def test_photo_settings_are_asserted_idempotently(occ_calls):
    hb.ensure_photo_settings()
    assert ("app:enable", "photos") in occ_calls
    assert ("config:system:set", "preview_max_x", "--value", "2048") in occ_calls
    assert ("config:system:set", "preview_max_y", "--value", "2048") in occ_calls


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
