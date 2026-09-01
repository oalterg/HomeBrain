"""Pure mapping of connect/token status → recoverable verdict."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import vault_account as va  # noqa: E402


def test_429_is_unknown_not_unrecoverable():
    assert va.probe_verdict(429) == "unknown"
    assert va.probe_verdict(429) != "not_recoverable"


def test_401_is_not_recoverable():
    assert va.probe_verdict(401) == "not_recoverable"


def test_timeout_is_unknown():
    assert va.probe_verdict(0) == "unknown"


def password_vault_action(verdict):
    """What /password does with the vault, in one place the UI copy can share."""
    if verdict == "recoverable":
        return "reset"
    if verdict == "not_recoverable":
        return "refuse_changed"
    return "refuse_unknown"


def test_password_route_refuses_unknown_with_a_different_sentence():
    assert password_vault_action("unknown") == "refuse_unknown"
    assert password_vault_action("not_recoverable") == "refuse_changed"
    assert password_vault_action("unknown") != password_vault_action("not_recoverable")
