#!/usr/bin/env python3
"""Asset wiring for the shared credential sheet (docs/plans/RECOVERY_SHEET.md L3).

test_creds_sheet.js proves the sheet text is right. This proves the browser can
actually reach the code that builds it: the module is served, both pages load it,
and neither page has quietly grown its own copy back. Dropping the script tag
would break the download silently — the button would just do nothing — and losing
the handover sheet costs a factory reset.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_creds_sheet_wiring.py
    pytest scripts/tests/test_creds_sheet_wiring.py
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
from flask import render_template  # noqa: E402

MODULE = "creds_sheet.js"
# The tell-tale of an inline re-implementation: no page but the shared module
# should be building a Blob URL.
INLINE_MARKER = "URL.createObjectURL"


@contextlib.contextmanager
def _client():
    """A logged-in test client, with every module global put back afterwards.

    These are process-wide: pytest collects this file before most others, and a
    leaked is_setup_complete() -> True would silently reshape every later suite.
    Same save/restore contract as the Harness in test_master_password.py.
    """
    saved = {
        "is_setup_complete": hb.is_setup_complete,
        "INSTALL_CREDS_PATH": hb.INSTALL_CREDS_PATH,
        "TESTING": hb.app.config.get("TESTING"),
    }
    hb.app.config["TESTING"] = True
    hb.is_setup_complete = lambda: True
    hb.INSTALL_CREDS_PATH = "/nonexistent"   # keep index() off the handover branch
    try:
        c = hb.app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
        yield c
    finally:
        hb.is_setup_complete = saved["is_setup_complete"]
        hb.INSTALL_CREDS_PATH = saved["INSTALL_CREDS_PATH"]
        hb.app.config["TESTING"] = saved["TESTING"]


def test_module_is_served():
    with _client() as c:
        r = c.get(f"/static/{MODULE}")
    assert r.status_code == 200
    assert b"function buildCredsSheet" in r.data
    assert b"function buildMemberSheet" in r.data
    assert b"function saveCredsSheet" in r.data


def test_dashboard_loads_module_before_dashboard_js():
    with _client() as c:
        html = c.get("/").get_data(as_text=True)
    assert MODULE in html
    # dashboard.js calls downloadCredsSheet(); the definition has to be parsed first.
    assert html.index(MODULE) < html.index("dashboard.js")
    assert "downloadChangedPassword()" in html      # the master-password sheet


def test_dashboard_js_has_no_builder_of_its_own():
    """The rendered page never contains dashboard.js's body, so asserting the
    marker is absent from the HTML would pass no matter what that file holds.
    Read the served script itself."""
    with _client() as c:
        js = c.get("/static/dashboard.js").get_data(as_text=True)
    assert INLINE_MARKER not in js
    assert "function downloadCredsSheet" not in js


def test_handover_page_loads_module_before_its_inline_script():
    with _client():
        with hb.app.test_request_context("/"):
            html = render_template("installing.html", handover_ready=True, restore_mode=False)
    assert MODULE in html
    assert html.index(MODULE) < html.index("downloadHandoverSheet")
    assert INLINE_MARKER not in html
    # The builder arrives in a separate request now; a silent no-op here ends
    # with cleanup_credentials deleting the only copy of the secrets.
    assert "typeof downloadCredsSheet !== 'function'" in html


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except AssertionError as e:
                print(f"FAIL  {name}: {e}")
                failed += 1
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    sys.exit(1 if failed else 0)
