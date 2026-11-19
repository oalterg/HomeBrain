"""MCP wiring goes through `openclaw config set|unset` and never leaves its home.

OpenClaw 2026.10 refuses `openclaw mcp set|unset` while the gateway runs, so
the old calls failed on every live box. And an E2E run with HOMEBRAIN_HOME on a
scratch copy unset the LIVE servers, because the CLI always edits the homebrain
user's own config.

Run: python3 -m pytest scripts/tests/test_mcp_wiring.py
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import integrations  # noqa: E402

SECRET = "2F7TuWo2rjYU5EdKER0y1YUC7iWuGrursbntInecC04="
SPEC = {"command": "/usr/bin/python3", "args": ["/opt/homebrain/scripts/mcp-email.py"],
        "env": {"HOMEBRAIN_EMAIL_KEY": SECRET}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A live-looking box: OPENCLAW_DIR is the homebrain user's own."""
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    monkeypatch.setattr(integrations, "OPENCLAW_DIR", str(oc))
    monkeypatch.setattr(integrations, "_live_openclaw_dir", lambda: str(oc))
    monkeypatch.setattr(integrations, "_has_openclaw", lambda: True)

    class Cli(list):
        rc, out = 0, ""

    cli = Cli()

    def fake_run(argv, **kw):
        batch = None
        if "--batch-file" in argv:
            with open(argv[argv.index("--batch-file") + 1]) as f:
                batch = json.load(f)
        cli.append({"argv": argv, "batch": batch})
        return subprocess.CompletedProcess(argv, cli.rc, cli.out, "")

    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    return cli


def test_set_writes_through_config_set_with_the_spec_in_a_file(box, tmp_path):
    ok, msg = integrations._openclaw_mcp_set("homebrain-email", SPEC)
    assert ok, msg
    (call,) = box
    assert call["argv"][:6] == ["sudo", "-u", "homebrain", "openclaw", "config", "set"]
    assert call["batch"] == [{"path": "mcp.servers.homebrain-email", "value": SPEC}]
    assert not any(SECRET in a for a in call["argv"]), "the key must not ride on argv"
    assert os.listdir(tmp_path / ".openclaw") == [], "batch file is removed"


def test_set_failure_is_reported_without_the_key(box):
    box.rc, box.out = 1, f"Config validation failed near {SECRET}"
    ok, msg = integrations._openclaw_mcp_set("homebrain-email", SPEC)
    assert not ok
    assert SECRET not in msg and "***" in msg


def test_unset_treats_an_absent_server_as_done(box):
    box.rc, box.out = 1, "Config path not found: mcp.servers.homebrain-email. Nothing was changed."
    assert integrations._openclaw_mcp_unset("homebrain-email") is True
    assert box[0]["argv"][4:] == ["config", "unset", "mcp.servers.homebrain-email"]


def test_unset_reports_a_real_failure(box):
    box.rc, box.out = 1, "Cannot admit config.unset"
    assert integrations._openclaw_mcp_unset("homebrain-email") is False


def test_scratch_home_never_touches_the_live_agent(box, monkeypatch, tmp_path):
    """The 2026-11-17 incident: no account files in the scratch home, five
    servers in its config copy -> reconcile unset HA, Nextcloud and email live."""
    scratch = tmp_path / "scratch" / ".openclaw"
    scratch.mkdir(parents=True)
    (scratch / "openclaw.json").write_text(json.dumps({"mcp": {"servers": {
        n: {} for n in integrations.MCP_NAMES.values()}}}))
    monkeypatch.setattr(integrations, "OPENCLAW_DIR", str(scratch))
    monkeypatch.setattr(integrations, "_OPENCLAW_CONFIG_PATH", str(scratch / "openclaw.json"))
    assert integrations._openclaw_isolated()
    assert integrations._openclaw_mcp_set("homebrain-email", SPEC)[0] is False
    assert integrations._openclaw_mcp_unset("homebrain-email") is False
    integrations._openclaw_daemon_restart()
    for key in ("homeassistant", "nextcloud", "email"):
        monkeypatch.setitem(integrations.SPEC_BUILDERS, key, lambda: None)
        integrations.reconcile_one(key)
    assert box == [], "no openclaw CLI call may leave the scratch home"


def test_no_homebrain_user_means_no_guard(monkeypatch):
    monkeypatch.setattr(integrations, "_live_openclaw_dir", lambda: None)
    assert not integrations._openclaw_isolated()
