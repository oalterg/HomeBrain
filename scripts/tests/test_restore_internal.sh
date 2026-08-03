#!/usr/bin/env bash
#
# Regression tests for common.sh:ensure_backup_dir — the shared mount guard for
# backup.sh and restore.sh.
#
# The bug this pins: restore.sh carried its own copy of the guard and never
# learned about BACKUP_INTERNAL (no-drive mode, added in #123). On a box with
# no backup drive the dashboard listed archives correctly and every restore
# died at "Backup drive not mounted." Back up, never restore.
#
# Same convention as test_update_guard.sh: the logic lives in common.sh so it
# can be exercised with no Docker, no network and no root.
#
#   bash scripts/tests/test_restore_internal.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== BACKUP_INTERNAL=true: no mountpoint required =="

# The regression. A path on the root disk that is not a mountpoint must be
# accepted, and created if absent.
target="$TMP/internal/backups"
if ( BACKUP_INTERNAL=true BACKUP_MOUNTDIR="$target" ensure_backup_dir ) 2>/dev/null; then
    ok "accepts a non-mountpoint path"
else
    bad "accepts a non-mountpoint path (expected success)"
fi
if [ -d "$target" ]; then
    ok "creates the directory when absent"
else
    bad "creates the directory when absent (not created)"
fi

# Idempotent: a second call on an existing directory is still fine.
if ( BACKUP_INTERNAL=true BACKUP_MOUNTDIR="$target" ensure_backup_dir ) 2>/dev/null; then
    ok "idempotent on an existing directory"
else
    bad "idempotent on an existing directory (expected success)"
fi

# An uncreatable path must fail loudly rather than let the caller write into
# nowhere. Skipped as root, which can create anything.
if [ "$(id -u)" -eq 0 ]; then
    printf '  skip  fails when the directory cannot be created (running as root)\n'
else
    blocked="$TMP/blocked"
    mkdir -p "$blocked"
    chmod 500 "$blocked"
    if ( BACKUP_INTERNAL=true BACKUP_MOUNTDIR="$blocked/sub" ensure_backup_dir ) 2>/dev/null; then
        bad "fails when the directory cannot be created (expected failure)"
    else
        ok "fails when the directory cannot be created"
    fi
    chmod 700 "$blocked"
fi

echo "== BACKUP_INTERNAL unset/false: mount check stays mandatory =="

# The invariant the guard exists to protect: a drive that fell off must never
# silently degrade into writing onto the root disk. An ordinary directory is
# not a mountpoint and is not in fstab, so the mount attempt must fail.
if ! command -v mountpoint >/dev/null 2>&1; then
    printf '  skip  rejects a non-mountpoint (no mountpoint(8) on this host)\n'
else
    plain="$TMP/plain"
    mkdir -p "$plain"
    if ( BACKUP_MOUNTDIR="$plain" ensure_backup_dir ) >/dev/null 2>&1; then
        bad "rejects a non-mountpoint when BACKUP_INTERNAL is unset (expected failure)"
    else
        ok "rejects a non-mountpoint when BACKUP_INTERNAL is unset"
    fi
    if ( BACKUP_INTERNAL=false BACKUP_MOUNTDIR="$plain" ensure_backup_dir ) >/dev/null 2>&1; then
        bad "rejects a non-mountpoint when BACKUP_INTERNAL=false (expected failure)"
    else
        ok "rejects a non-mountpoint when BACKUP_INTERNAL=false"
    fi
fi

echo "== ensure_staging_dir: an off-site restore needs a landing site, not a drive =="

# The bug this pins: the wizard's restore-from-off-site called ensure_backup_dir
# first, so a box with no backup drive died at "Failed to mount backup drive"
# before the fetch started — on the one recovery path whose whole premise is
# that the drive is gone. The E2E missed it because nuclear_reset preserves
# /mnt/backup's fstab entry, so the wiped test box still had a drive to mount.
if ! command -v mountpoint >/dev/null 2>&1; then
    printf '  skip  falls back to the internal disk (no mountpoint(8) on this host)\n'
else
    plain="$TMP/absent"
    mkdir -p "$plain"
    internal="$TMP/var-backups"
    staged="$(
        BACKUP_INTERNAL=false
        BACKUP_MOUNTDIR="$plain"
        INTERNAL_BACKUP_DIR="$internal"
        ensure_staging_dir 2>/dev/null
        echo "$BACKUP_MOUNTDIR"
    )"
    if [ "$staged" = "$internal" ]; then
        ok "falls back to the internal disk when the drive is absent"
    else
        bad "falls back to the internal disk when the drive is absent (got '${staged:-<nothing>}')"
    fi
    if [ -d "$internal" ]; then
        ok "creates the fallback directory"
    else
        bad "creates the fallback directory (not created)"
    fi
fi

# No fallback when the configured location is already usable: an archive lands
# on the drive when there is one, and a no-drive box keeps its own setting.
target="$TMP/internal/backups"
staged="$(
    BACKUP_INTERNAL=true
    BACKUP_MOUNTDIR="$target"
    INTERNAL_BACKUP_DIR="$TMP/never"
    ensure_staging_dir 2>/dev/null
    echo "$BACKUP_MOUNTDIR"
)"
if [ "$staged" = "$target" ]; then
    ok "keeps BACKUP_MOUNTDIR when it is already usable"
else
    bad "keeps BACKUP_MOUNTDIR when it is already usable (got '${staged:-<nothing>}')"
fi
if [ -d "$TMP/never" ]; then
    bad "does not touch the fallback when it is not needed (created it anyway)"
else
    ok "does not touch the fallback when it is not needed"
fi

echo "== both callers use the shared guard =="

# Cheap structural check: if either script grows its own copy of the mount
# logic again, this fails and points at why.
for script in backup.sh restore.sh; do
    if grep -q 'ensure_backup_dir' "$SCRIPT_DIR/../$script"; then
        ok "$script calls ensure_backup_dir"
    else
        bad "$script calls ensure_backup_dir (not found)"
    fi
    if grep -q 'mountpoint -q "\$BACKUP_MOUNTDIR"' "$SCRIPT_DIR/../$script"; then
        bad "$script has its own mount check again"
    else
        ok "$script has no private mount check"
    fi
done

# The wiring, not just the helper: calling the wrong one of the two is the bug.
if grep -q 'ensure_staging_dir' "$SCRIPT_DIR/../restore.sh"; then
    ok "restore.sh stages off-site fetches instead of demanding the drive"
else
    bad "restore.sh stages off-site fetches instead of demanding the drive (not found)"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
