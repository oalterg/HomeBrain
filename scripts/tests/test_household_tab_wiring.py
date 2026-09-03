#!/usr/bin/env python3
"""The Household tab is wired to the code that fills it.

Household used to be a card in Connectivity, which answers "how do devices reach
this box?" — the wrong question for "who lives here?". It now has its own tab,
and the move has a silent failure mode: leave `loadHousehold()` behind in the
connectivity branch of `openTab` and the tab still opens, still renders the
card, and sits on its loading skeleton forever with nothing in the console.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_household_tab_wiring.py
    pytest scripts/tests/test_household_tab_wiring.py
"""
import contextlib
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_JS = os.path.join(HERE, os.pardir, os.pardir, "src", "static", "dashboard.js")


@contextlib.contextmanager
def _client():
    """A logged-in test client, with every module global put back afterwards.

    Same save/restore contract as test_creds_sheet_wiring.py — these are
    process-wide and a leaked is_setup_complete() reshapes every later suite.
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


def _dashboard_html():
    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_every_tab_button_has_a_panel():
    """A button with no panel is a dead tab; a panel with no button is dead code."""
    html = _dashboard_html()
    buttons = re.findall(r'tab-btn[^>]*data-tab="([a-z]+)"', html)
    panels = re.findall(r'<div id="([a-z]+)" class="tab-content', html)
    assert buttons == panels, f"tab bar {buttons} does not match panels {panels}"
    assert "household" in buttons


def test_household_card_lives_in_the_household_panel():
    html = _dashboard_html()
    assert html.count('id="household-card"') == 1
    starts = [m.start() for m in re.finditer(r'<div id="[a-z]+" class="tab-content', html)]
    panel = html.index('<div id="household" class="tab-content')
    nxt = next(s for s in starts if s > panel)   # the panel that follows it
    card = html.index('id="household-card"')
    assert panel < card < nxt, "household card is not inside the household panel"


def test_connectivity_carries_no_member_markup():
    """Connectivity keeps Tunnel/Network/FTP. Anything member-shaped is residue."""
    html = _dashboard_html()
    start = html.index('<div id="connectivity" class="tab-content')
    end = html.index('<div id="settings" class="tab-content')
    section = html[start:end]
    for marker in ("household", "member-name", "member-pair"):
        assert marker not in section, f"{marker!r} left behind in Connectivity"


def test_opening_the_tab_loads_the_roster():
    """openTab('household') must call loadHousehold(), or the tab never fills."""
    with open(DASHBOARD_JS, encoding="utf-8") as fh:
        js = fh.read()
    body = js[js.index("function openTab("):]
    body = body[:body.index("\n}\n")]
    assert re.search(r"id === 'household'[^\n]*loadHousehold\(\)", body), \
        "openTab does not call loadHousehold() for the household tab"
    conn = re.search(r"id === 'connectivity'\) \{(.*?)\}", body, re.S)
    assert conn and "loadHousehold" not in conn.group(1), \
        "loadHousehold() is still wired to the connectivity tab"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all wiring checks passed")
