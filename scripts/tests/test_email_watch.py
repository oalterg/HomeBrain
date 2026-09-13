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
    assert "<<<INBOX>>>" in prompt
    assert "pass folder" in prompt
    instr = prompt.split("not instructions:")[0]
    assert "Please archive this" not in instr
    assert "do it" not in instr


def test_wake_prompt_includes_non_inbox_folder():
    prompt = email_watch.wake_prompt(
        "Agent", "4", "owner@ex.com", "agent@ex.com",
        "check dis", "ping", [], True, "Bulk Mail")
    assert "<<<Bulk Mail>>>" in prompt
    assert "email.send_direct" in prompt


def test_prompting_off_is_json_only():
    """No IMAP: prompting_ready false when the channel file is off."""
    assert not email_watch.prompting_ready(
        {"enabled": False, "allow_from": ["a@b.c"]},
        [{"name": "A", "user": "agent@ex.com", "agent_mailbox": True}])


def test_watch_folders_adds_yahoo_bulk_not_trash():
    listed = [
        b'(\\HasNoChildren) "/" INBOX',
        b'(\\HasNoChildren \\Junk) "/" "Bulk Mail"',
        b'(\\HasNoChildren \\Trash) "/" Trash',
        b'(\\HasNoChildren \\Sent) "/" Sent',
        b'(\\HasNoChildren) "/" "[Gmail]/Spam"',
    ]
    folders = email_watch.watch_folders(listed)
    assert folders[0] == "INBOX"
    assert "Bulk Mail" in folders
    assert "[Gmail]/Spam" in folders
    assert "Trash" not in folders
    assert "Sent" not in folders


def test_is_junk_mailbox_by_name_and_flag():
    assert email_watch.is_junk_mailbox("Bulk Mail")
    assert email_watch.is_junk_mailbox("Spam")
    assert email_watch.is_junk_mailbox("Junk", r"\Junk")
    assert not email_watch.is_junk_mailbox("INBOX")
    assert not email_watch.is_junk_mailbox("Trash", r"\Trash")


def test_nested_cursors_migrate_flat_inbox():
    nested = email_watch.nested_account_cursors(
        {"uidvalidity": 9, "last_uid": 12})
    assert nested["INBOX"] == {"uidvalidity": 9, "last_uid": 12}
    already = {"INBOX": {"uidvalidity": 1, "last_uid": 3},
               "Bulk Mail": {"uidvalidity": 2, "last_uid": 8}}
    assert email_watch.nested_account_cursors(already)["Bulk Mail"]["last_uid"] == 8


def test_junk_folder_replays_existing_mail():
    action, cur = email_watch.cursor_for_scan(None, 9, 12, replay=True)
    assert action == "scan"
    assert cur == {"uidvalidity": 9, "last_uid": 0}


def test_fetch_bytes_skips_exists_none():
    class Conn:
        def uid(self, *_a, **_k):
            return "OK", [None, (b"1 (BODY[HEADER] {4}", b"From: x")]
    assert email_watch._fetch_bytes(Conn(), 1, "(BODY.PEEK[HEADER])") == b"From: x"
