"""Unit tests for owner-to-agent email watch (no live IMAP).

Run: python3 -m pytest scripts/tests/test_email_watch.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import email_watch  # noqa: E402


def test_normalize_parseaddr():
    assert email_watch.normalize_email('Oliver <Owner@Ex.COM>') == "owner@ex.com"


def test_uid_search_is_not_unseen():
    spec = email_watch.uid_search_set(41)
    assert spec == "42:*"
    assert "UNSEEN" not in spec
    args = email_watch.uid_search_args(41)
    assert "None" not in args
    assert None not in args
    assert "UNSEEN" not in args
    assert args == ("UID", "42:*")


def test_uids_after_drops_star_substitution():
    # Servers may return the mailbox max for n:* when n does not exist.
    assert email_watch.uids_after([41], 41) == []
    assert email_watch.uids_after([41, 42], 41) == [42]
    assert email_watch.uids_after([1, 2], 0) == [1, 2]


def test_auth_fail_does_not_tight_loop():
    assert email_watch.auth_fail_wait(1) == 60
    assert email_watch.auth_fail_wait(2) == 180
    assert email_watch.auth_fail_wait(3) == 600
    assert email_watch.auth_fail_wait(9) == 600
    assert email_watch.auth_fail_wait(0) >= 60


def test_cursor_seeds_on_missing_or_validity_change():
    action, cur = email_watch.cursor_for_scan(None, 9, 12)
    assert action == "seed"
    assert cur == {"uidvalidity": 9, "last_uid": 12}

    action, cur = email_watch.cursor_for_scan(
        {"uidvalidity": 1, "last_uid": 5}, 2, 20)
    assert action == "seed"
    assert cur["last_uid"] == 20

    action, cur = email_watch.cursor_for_scan(
        {"uidvalidity": 1, "last_uid": 5}, 1, 20)
    assert action == "scan"
    assert cur["last_uid"] == 5


def test_should_wake_allowlist_and_agent_from():
    allow = ["owner@ex.com"]
    agents = ["agent@ex.com"]
    assert email_watch.should_wake("Owner <owner@ex.com>", allow, agents)
    assert not email_watch.should_wake("other@ex.com", allow, agents)
    assert not email_watch.should_wake("agent@ex.com", allow, agents)
    assert not email_watch.should_wake(
        "owner@ex.com", allow, agents,
        {"auto-submitted": "auto-replied"})


def test_strip_quoted_drops_history():
    body = "Do the thing\n> old quote\nOn Tue, Bob wrote:\n> more"
    assert email_watch.strip_quoted(body) == "Do the thing"


def test_prompting_ready_requires_flag_and_from_neq_to():
    accounts = [{"name": "A", "user": "agent@ex.com", "agent_mailbox": True}]
    assert not email_watch.prompting_ready(
        {"enabled": False, "allow_from": ["owner@ex.com"]}, accounts)
    assert not email_watch.prompting_ready(
        {"enabled": True, "allow_from": ["agent@ex.com"]}, accounts)
    assert email_watch.prompting_ready(
        {"enabled": True, "allow_from": ["owner@ex.com"]}, accounts)
    assert not email_watch.prompting_ready(
        {"enabled": True, "allow_from": ["owner@ex.com"]},
        [{"name": "P", "user": "owner@ex.com", "agent_mailbox": False}])


def test_pick_wake_serializes():
    cands = [{"uid": 1}, {"uid": 2}]
    assert email_watch.pick_wake(cands, False) == {"uid": 1}
    assert email_watch.pick_wake(cands, True) is None
    assert email_watch.pick_wake([], False) is None


def test_wake_argv_isolated_session_no_deliver():
    argv = email_watch.wake_argv("prompt", "telegram", "123")
    assert argv[argv.index("--session-key") + 1] == "email-in"
    assert "--deliver" not in argv
    assert "--isolated" not in argv
    assert argv[argv.index("--channel") + 1] == "telegram"
    assert argv[argv.index("--to") + 1] == "123"
    assert "agent" in argv
    bare = email_watch.wake_argv("prompt")
    assert "--channel" not in bare


def test_wake_prompt_wraps_and_tells_draft():
    prompt = email_watch.wake_prompt(
        "Agent", "7", "owner@ex.com", "agent@ex.com",
        "Please archive this", "do it\n> quoted", ["scan.pdf"], False)
    assert "not instructions" in prompt
    assert "<<<Please archive this>>>" in prompt
    assert "<<<do it>>>" in prompt
    assert "quoted" not in prompt.split("not instructions:")[1]
    assert "email.draft" in prompt
    assert "will not send" in prompt
    instr = prompt.split("not instructions:")[0]
    assert "Please archive this" not in instr
    assert "do it" not in instr


def test_prompting_off_is_json_only():
    """No IMAP: prompting_ready false when the channel file is off."""
    assert not email_watch.prompting_ready(
        {"enabled": False, "allow_from": ["a@b.c"]},
        [{"name": "A", "user": "agent@ex.com", "agent_mailbox": True}])
