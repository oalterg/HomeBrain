#!/usr/bin/env python3
"""Tests for the first-boot wizard: GPU vs HomeCloud copy, factory tunnel.

HomeBrain (GPU): local is the default; Telegram is the remote path; Pangolin
is optional and is not auto-selected even when factory_config has Newt creds.

HomeCloud (no GPU): Pangolin is the remote path; factory Newt creds auto-select
Remote access; the secret stays on the box (not rendered) and the page tells
the owner they can leave it blank.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_welcome_wizard.py
    pytest scripts/tests/test_welcome_wizard.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

FACTORY_TUNNEL = {
    "NEWT_ID": "newt-factory",
    "NEWT_SECRET": "factory-secret-value",
    "PANGOLIN_ENDPOINT": "https://pan.example",
    "PANGOLIN_DOMAIN": "box.example.com",
}


class WelcomeHarness:
    def __init__(self, factory=None, has_gpu=False):
        self._saved_env = os.environ.get("HAS_GPU")
        os.environ["HAS_GPU"] = "true" if has_gpu else "false"

        self._saved = {k: getattr(hb, k) for k in (
            "INSTALL_CREDS_PATH", "SETUP_STARTED_MARKER", "RESTORING_MARKER",
            "is_setup_complete", "get_factory_config")}
        self._saved_limiter = hb.limiter.enabled

        # Point markers at paths that do not exist so index() renders welcome.
        self._nonesuch = tempfile.mkdtemp(prefix="hb_welcome_")
        hb.INSTALL_CREDS_PATH = os.path.join(self._nonesuch, "install_creds.json")
        hb.SETUP_STARTED_MARKER = os.path.join(self._nonesuch, ".setup_started")
        hb.RESTORING_MARKER = os.path.join(self._nonesuch, ".restoring")
        hb.is_setup_complete = lambda: False
        factory = dict(factory or {})
        hb.get_factory_config = lambda: factory
        hb.limiter.enabled = False
        hb.app.config["TESTING"] = True
        self.client = hb.app.test_client()

    def get(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        return self.client.get("/")

    def close(self):
        if self._saved_env is None:
            os.environ.pop("HAS_GPU", None)
        else:
            os.environ["HAS_GPU"] = self._saved_env
        for k, v in self._saved.items():
            setattr(hb, k, v)
        hb.limiter.enabled = self._saved_limiter
        try:
            os.rmdir(self._nonesuch)
        except OSError:
            pass


def test_homebrain_leads_with_telegram_not_pangolin():
    h = WelcomeHarness(has_gpu=True)
    try:
        r = h.get()
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "pair Telegram" in html
        assert "Public tunnel" in html
        assert "selectMode('remote');" not in html
        assert "There is no agent on this box" not in html
    finally:
        h.close()


def test_homebrain_does_not_auto_select_factory_tunnel():
    h = WelcomeHarness(factory=FACTORY_TUNNEL, has_gpu=True)
    try:
        html = h.get().get_data(as_text=True)
        assert "The agent does not need them" in html
        assert "selectMode('remote');" not in html
        assert "factoryHasSecret = true" in html
        assert "factory-secret-value" not in html
    finally:
        h.close()


def test_homecloud_auto_selects_factory_tunnel():
    h = WelcomeHarness(factory=FACTORY_TUNNEL, has_gpu=False)
    try:
        html = h.get().get_data(as_text=True)
        assert "There is no agent on this box" in html
        assert "selectMode('remote');" in html
        assert "Leave the secret blank" in html
        assert "factoryHasSecret = true" in html
        assert "factory-secret-value" not in html
        assert "Leave blank to use the factory secret" in html
    finally:
        h.close()


def test_homecloud_without_factory_does_not_auto_select_remote():
    h = WelcomeHarness(has_gpu=False)
    try:
        html = h.get().get_data(as_text=True)
        assert "selectMode('remote');" not in html
        assert "factoryHasSecret = false" in html
        assert "Remote access" in html
    finally:
        h.close()


def test_device_id_placeholder_invents_no_prefix():
    """Pangolin newt IDs carry no 'newt-' prefix.

    The placeholder used to show one, so an owner comparing it against the ID
    Pangolin had actually given them had to decide which of the two was wrong.
    """
    h = WelcomeHarness(has_gpu=False)
    try:
        html = h.get().get_data(as_text=True)
        assert "newt-xxxxxxxx" not in html
        assert "The Newt ID from your Pangolin site" in html
    finally:
        h.close()


def test_main_domain_field_shows_the_derived_hostnames():
    """The three public hostnames are this field with a label prefixed on.

    Typing a service hostname here (nc.example.com) silently yields
    nc.nc.example.com, and the failure surfaces only much later as Nextcloud
    refusing its own tunnel as an untrusted domain.
    """
    h = WelcomeHarness(has_gpu=False)
    try:
        html = h.get().get_data(as_text=True)
        assert "function previewDomains()" in html
        assert 'id="domain-preview"' in html
        assert "oninput=\"previewDomains()\"" in html
        assert "without a prefix" in html
    finally:
        h.close()


def test_refresh_during_a_restore_does_not_hand_over_credentials():
    """The bug this pins, seen on a real bare-metal restore.

    deploy.sh is only the first half of a restore install, and it touches
    .setup_complete before restore.sh has written a single byte of the owner's
    data. Refreshing the wizard in that window used to land on the credentials
    screen — one click from cleanup_credentials, which starts the tunnels and
    deletes the marker, mid-restore.
    """
    h = WelcomeHarness(has_gpu=False)
    try:
        # Setup is "complete" as far as deploy.sh is concerned, and the restore
        # is still running.
        hb.is_setup_complete = lambda: True
        open(hb.RESTORING_MARKER, "w").write("hb-backup.tar.gz.gpg")

        html = h.get().get_data(as_text=True)
        # The progress screen, in restore wording, with the handover withheld.
        assert 'id="phase"' in html
        assert "const restoreMode = true" in html
        assert "const handoverReady = false" in html
    finally:
        if os.path.exists(hb.RESTORING_MARKER):
            os.remove(hb.RESTORING_MARKER)
        h.close()


def test_every_progress_phase_matches_a_line_a_script_really_prints():
    """The progress screen reads the setup log and names the current phase.

    Nothing links the two: a reworded log_info silently stops the phase from
    advancing, and the owner is back to watching a screen that says nothing is
    happening. Every marker must be a substring the shell actually emits.
    """
    root = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    page = os.path.join(root, "src", "templates", "installing.html")
    with open(page) as f:
        markers = re.findall(r"^\s*\['([^']+)',", f.read(), re.M)
    assert markers, "no PHASES table found in installing.html"

    shell = ""
    for name in ("deploy.sh", "restore.sh", "common.sh"):
        with open(os.path.join(root, "scripts", name)) as f:
            shell += f.read()

    missing = [m for m in markers if m not in shell]
    assert not missing, f"phase markers no script prints any more: {missing}"


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
