#!/usr/bin/env python3
"""Tests for the off-site upload progress the dashboard shows.

A full archive takes hours to push over a home uplink, and the Backup page used
to say only "Off-site sync in progress…" for the whole run — indistinguishable
from a wedged transfer. rclone reports progress solely to its log, so app.py
parses the stats lines back out.

The bug this pins: nothing rotates backup.log, so stats lines from previous
mirrors sit in it forever. Reading the last one unconditionally means a mirror
in its first minute — before rclone has written any line of its own — reports
the PREVIOUS run's percentage as if it were live. A freshly started upload
would show "97%, ETA 2m" and then appear to go backwards.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_offsite_progress.py
    pytest scripts/tests/test_offsite_progress.py
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

# Real lines, copied from a live mirror on the production box.
LINE = ("2026/08/12 20:28:10 NOTICE:    54.686 GiB / 97.608 GiB, "
        "56%, 4.047 MiB/s, ETA 3h1m1s")
OLDER = ("2026/08/11 09:02:00 NOTICE:    95.100 GiB / 97.608 GiB, "
         "97%, 3.900 MiB/s, ETA 2m0s")


def _log(tmp, text):
    """Point app.py's log reader at a scratch backup.log holding `text`."""
    hb.LOG_DIR = tmp
    with open(os.path.join(tmp, "backup.log"), "w") as f:
        f.write(text)


def _stamp(line):
    """The epoch seconds of a stats line's own timestamp."""
    return datetime.strptime(line.split(" NOTICE:")[0], "%Y/%m/%d %H:%M:%S").timestamp()


def test_parses_a_live_stats_line():
    with tempfile.TemporaryDirectory() as tmp:
        _log(tmp, f"[INFO] Mirroring backups off-site (webdav)...\n{LINE}\n")
        p = hb.offsite_progress(_stamp(LINE) - 60)
        assert p is not None, "a stats line from this run should be reported"
        assert p["percent"] == 56, p
        assert p["done"] == "54.686 GiB" and p["total"] == "97.608 GiB", p
        assert p["speed"] == "4.047 MiB/s", p
        assert p["eta"] == "3h1m1s", p


def test_ignores_stats_left_over_from_an_earlier_mirror():
    """The regression: last run's 97% must not surface as this run's progress."""
    with tempfile.TemporaryDirectory() as tmp:
        _log(tmp, f"{OLDER}\n[INFO] Mirroring backups off-site (webdav)...\n")
        # This mirror started after that line was written.
        assert hb.offsite_progress(_stamp(OLDER) + 3600) is None


def test_reports_the_newest_line_when_several_runs_are_in_the_log():
    with tempfile.TemporaryDirectory() as tmp:
        _log(tmp, f"{OLDER}\n{LINE}\n")
        p = hb.offsite_progress(_stamp(LINE) - 60)
        assert p is not None and p["percent"] == 56, p


def test_survives_an_eta_rclone_cannot_estimate():
    with tempfile.TemporaryDirectory() as tmp:
        line = ("2026/08/12 20:28:10 NOTICE:    1.000 GiB / 97.608 GiB, "
                "1%, 0.000 B/s, ETA -")
        _log(tmp, line + "\n")
        p = hb.offsite_progress(_stamp(line) - 60)
        assert p is not None and p["eta"] is None, p


def test_quiet_when_rclone_has_not_reported_yet():
    """The first stats line lands a minute in; until then there is no bar."""
    with tempfile.TemporaryDirectory() as tmp:
        _log(tmp, "[INFO] Mirroring backups off-site (webdav)...\n")
        assert hb.offsite_progress(0) is None


def test_quiet_without_a_log_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        hb.LOG_DIR = os.path.join(tmp, "nope")
        assert hb.offsite_progress(0) is None


def test_finds_a_line_past_the_64k_tail_window():
    """Only the tail is read; the fragment it starts mid-line must not break it."""
    with tempfile.TemporaryDirectory() as tmp:
        _log(tmp, ("x" * 200_000) + "\n" + LINE + "\n")
        p = hb.offsite_progress(_stamp(LINE) - 60)
        assert p is not None and p["percent"] == 56, p


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failures += 1
    print("PASS" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
