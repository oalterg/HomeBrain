#!/usr/bin/env python3
"""Tests that a factory reset cannot hide the owner's backups.

The requirement: a factory reset must not wipe backups, and restoring from one
afterwards must work. It did not.

A reset deletes .env; the wizard regenerates it from the template. The no-drive
setting (BACKUP_INTERNAL / BACKUP_MOUNTDIR) goes with it, so a box that had been
keeping archives in /var/backups/homebrain came back looking only at
/mnt/backup and reported no backups at all — while the archives sat untouched
one directory away. Observed on the test box: 8 archives on disk, 4 listed.

These tests patch app module globals. They must restore them: leaving
get_env_config as a stub (the previous helper did) made every later test that
reads MASTER_PASSWORD or the recovery keys fail when pytest ran the suite.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_backup_discovery.py
    pytest scripts/tests/test_backup_discovery.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402

ARCHIVE = "homebrain_backup_2026-08-02_18-01-21.tar.gz.gpg"


@contextmanager
def _dirs(mount="none"):
    """Point the app at a drive dir and an internal dir, then put it back.

    mount: "none" (drive default), "drive", or "internal" — the configured
    BACKUP_MOUNTDIR. The other location still exists so find_backup can see
    archives a factory reset would otherwise hide.
    """
    with tempfile.TemporaryDirectory() as tmp:
        drive = os.path.join(tmp, "mnt-backup")
        internal = os.path.join(tmp, "var-backups")
        os.makedirs(drive)
        os.makedirs(internal)
        configured = {"none": None, "drive": drive, "internal": internal}[mount]
        saved = hb.BACKUP_DIR, hb.INTERNAL_BACKUP_DIR, hb.get_env_config
        hb.BACKUP_DIR = drive
        hb.INTERNAL_BACKUP_DIR = internal
        hb.get_env_config = lambda: {"BACKUP_MOUNTDIR": configured}
        try:
            yield drive, internal
        finally:
            hb.BACKUP_DIR, hb.INTERNAL_BACKUP_DIR, hb.get_env_config = saved


def test_finds_an_archive_left_in_the_internal_dir():
    """The reset case: configured for the drive, archive kept internally."""
    with _dirs() as (_, internal):
        open(os.path.join(internal, ARCHIVE), "w").close()
        assert hb.find_backup(ARCHIVE) == os.path.join(internal, ARCHIVE)


def test_finds_an_archive_on_the_drive():
    with _dirs() as (drive, _):
        open(os.path.join(drive, ARCHIVE), "w").close()
        assert hb.find_backup(ARCHIVE) == os.path.join(drive, ARCHIVE)


def test_configured_location_wins_over_the_other():
    """Two copies of one name must resolve to the location in use, so a
    restore reads the archive the owner is actually looking at."""
    with _dirs(mount="internal") as (drive, internal):
        for d in (drive, internal):
            with open(os.path.join(d, ARCHIVE), "w") as f:
                f.write(d)
        assert hb.find_backup(ARCHIVE) == os.path.join(internal, ARCHIVE)


def test_missing_archive_is_not_invented():
    with _dirs():
        assert hb.find_backup(ARCHIVE) is None


def test_search_dirs_are_deduped_and_must_exist():
    with _dirs(mount="drive") as (drive, internal):
        dirs = hb.backup_search_dirs()
        assert dirs.count(drive) == 1
        assert internal in dirs
        assert all(os.path.isdir(d) for d in dirs)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failures += 1
    sys.exit(1 if failures else 0)
