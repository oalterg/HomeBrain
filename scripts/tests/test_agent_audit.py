#!/usr/bin/env python3
"""Tests for the agent's log surface in the dashboard.

Two bugs motivated this file, both found on hardware (.58) on 2026-08-23:

  1. The Logs tab ran `journalctl -u openclaw`. No such unit exists — the
     gateway is the *user* unit `openclaw-gateway`. journalctl prints
     "-- No entries --" and exits 0, so the pane was silently empty and the
     agent was unobservable from the dashboard.
  2. The five MCP audit logs had no surface at all.

The subtle requirement is the level token. renderLogs() in dashboard.js
classifies a line by finding ERROR/WARN/INFO in it; a line with none falls
under "Debug / raw", which ships unchecked. Serving raw JSON would render an
empty console — the same silent-empty failure, one layer up. So every emitted
line must carry a level.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    """Point the app at a throwaway log dir.

    monkeypatch, not direct assignment: `app` is one entry in sys.modules for
    the whole session, and a global left rebound here leaks into every later
    test file (see test_backup_discovery.py for what that costs).
    """
    monkeypatch.setattr(hb, "LOG_DIR", str(tmp_path))
    return tmp_path


def write_audit(audit_dir, server, records):
    path = audit_dir / f"mcp-{server}-audit.log"
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def rec(ts, server, action, **fields):
    return {"ts": ts, "server": server, "action": action, **fields}


# --- journal selection -----------------------------------------------------

def test_openclaw_selects_the_user_unit_not_a_system_unit():
    """`-u openclaw` matched nothing and returned an empty pane."""
    sel = hb.journal_match("openclaw")
    assert "_SYSTEMD_USER_UNIT=openclaw-gateway.service" in sel
    assert "-u" not in sel


def test_the_uid_narrows_the_match_when_the_user_exists(monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": 1001})())
    assert "_UID=1001" in hb.journal_match("openclaw")


def test_a_missing_homebrain_user_degrades_instead_of_erroring(monkeypatch):
    """Dev boxes and CI runners have no `homebrain`. The unit name alone still
    selects the right journal, so the pane must not go to an error string."""
    import pwd
    def boom(_name):
        raise KeyError("getpwnam(): name not found: 'homebrain'")
    monkeypatch.setattr(pwd, "getpwnam", boom)
    assert hb.journal_match("openclaw") == ["_SYSTEMD_USER_UNIT=openclaw-gateway.service"]


def test_other_targets_still_use_a_plain_system_unit():
    assert hb.journal_match("llama-server") == ["-u", "llama-server"]


# --- merged audit view -----------------------------------------------------

def test_records_from_several_servers_merge_in_timestamp_order(audit_dir):
    write_audit(audit_dir, "vault", [
        rec("2026-08-23T10:00:00+00:00", "vault", "search", query="miami"),
    ])
    write_audit(audit_dir, "nextcloud", [
        rec("2026-08-23T09:00:00+00:00", "nextcloud", "download", path="/a.pdf"),
    ])
    write_audit(audit_dir, "homeassistant", [
        rec("2026-08-23T11:00:00+00:00", "homeassistant", "call_service.ok"),
    ])
    lines = hb.read_agent_audit().splitlines()
    assert [ln.split()[2] for ln in lines] == [
        "nextcloud.download", "vault.search", "homeassistant.call_service.ok",
    ]


def test_every_line_carries_a_level_token(audit_dir):
    """Without one the console renders empty under the default filters."""
    write_audit(audit_dir, "vault", [
        rec("2026-08-23T10:00:00+00:00", "vault", "search", query="x"),
        rec("2026-08-23T10:00:01+00:00", "vault", "reveal", item_name="GitHub"),
    ])
    for line in hb.read_agent_audit().splitlines():
        assert any(lv in line for lv in ("ERROR", "WARN", "INFO")), line


@pytest.mark.parametrize("action,level", [
    ("send.fail", "ERROR"),
    ("imap_error", "ERROR"),
    ("call_service.fail", "ERROR"),
    ("attachment.too_large", "WARN"),
    ("download.too_large", "WARN"),
    ("send", "INFO"),
    ("camera_image.ok", "INFO"),
    ("watcher_set", "INFO"),
])
def test_failures_classify_so_they_can_be_filtered(audit_dir, action, level):
    write_audit(audit_dir, "email", [
        rec("2026-08-23T10:00:00+00:00", "email", action),
    ])
    assert hb.read_agent_audit().split()[1] == level


def test_extra_fields_are_kept_on_the_line(audit_dir):
    write_audit(audit_dir, "nextcloud", [
        rec("2026-08-23T10:00:00+00:00", "nextcloud", "download",
            account="OliAidana", path="/Dokumente/SO101.pdf", bytes=404931),
    ])
    line = hb.read_agent_audit()
    assert "account=OliAidana" in line
    assert "path=/Dokumente/SO101.pdf" in line
    assert "bytes=404931" in line


def test_a_multiline_field_value_stays_on_one_line(audit_dir):
    """Found on .58 2026-08-23. Home Assistant records the upstream body on a
    failed call, and a 500 page carries newlines:

        {"action": "call_service_raw.fail", ...,
         "resp": "500 Internal Server Error\\n\\nServer got itself in trouble"}

    json.loads turns those back into real newlines, so interpolating the value
    split one record across three console lines — and the two continuation
    lines carry no level token, so the default filters hid them while the
    parent line showed. A line-oriented log surface must emit one line per
    record.
    """
    write_audit(audit_dir, "homeassistant", [
        rec("2026-08-23T10:00:00+00:00", "homeassistant", "call_service_raw.fail",
            code=500, resp="500 Internal Server Error\n\nServer got itself in trouble"),
    ])
    out = hb.read_agent_audit()
    assert len(out.splitlines()) == 1, out
    assert "Server got itself in trouble" in out
    assert out.startswith("2026-08-23T10:00:00+00:00 ERROR ")


def test_a_torn_line_does_not_blank_the_whole_view(audit_dir):
    path = audit_dir / "mcp-vault-audit.log"
    with open(path, "w") as f:
        f.write(json.dumps(rec("2026-08-23T10:00:00+00:00", "vault", "search")) + "\n")
        f.write('{"ts": "2026-08-23T10:00:01+00:00", "server": "vau\n')   # truncated
        f.write(json.dumps(rec("2026-08-23T10:00:02+00:00", "vault", "reveal")) + "\n")
    lines = hb.read_agent_audit().splitlines()
    assert len(lines) == 2
    assert "vault.search" in lines[0]
    assert "vault.reveal" in lines[1]


def test_a_server_that_never_ran_is_skipped(audit_dir):
    write_audit(audit_dir, "vault", [
        rec("2026-08-23T10:00:00+00:00", "vault", "search"),
    ])
    assert "vault.search" in hb.read_agent_audit()


def test_no_activity_says_so_rather_than_returning_nothing(audit_dir):
    """An empty console is indistinguishable from a failed fetch."""
    out = hb.read_agent_audit()
    assert out.strip()
    assert "No agent activity" in out


def test_last_weeks_rotated_records_still_show(audit_dir):
    """Right after logrotate the live file is empty and the records are in .1."""
    (audit_dir / "mcp-homeassistant-audit.log.1").write_text(
        json.dumps(rec("2026-11-15T10:00:00+00:00", "homeassistant", "call_service.ok")) + "\n")
    (audit_dir / "mcp-homeassistant-audit.log").write_text("")
    write_audit(audit_dir, "vault", [rec("2026-11-16T10:00:00+00:00", "vault", "search")])
    lines = hb.read_agent_audit().splitlines()
    assert [l.split()[2] for l in lines] == ["homeassistant.call_service.ok", "vault.search"]


def test_the_view_is_capped_and_keeps_the_newest(audit_dir):
    write_audit(audit_dir, "homeassistant", [
        rec(f"2026-08-23T10:{i // 60:02d}:{i % 60:02d}+00:00",
            "homeassistant", "call_service.ok", n=i)
        for i in range(500)
    ])
    lines = hb.read_agent_audit(limit=10).splitlines()
    assert len(lines) == 10
    assert "n=499" in lines[-1]


# --- audit write failures --------------------------------------------------

def test_a_failed_audit_write_is_announced_on_stderr(tmp_path, monkeypatch, capsys):
    """The dashboard reads the files; a silent write failure would look like a
    quiet day. stderr reaches the gateway journal, which the Logs tab reads."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
    import mcp_common

    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    monkeypatch.setattr(mcp_common, "AUDIT_DIR", str(unwritable / "sub"))
    monkeypatch.setattr(mcp_common.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    mcp_common.audit("vault", "reveal", item_name="GitHub")

    err = capsys.readouterr().err
    assert '"action": "audit.write_failed"' in err
    # the original record still went out first, so nothing is lost
    assert '"action": "reveal"' in err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
