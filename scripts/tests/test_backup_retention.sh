#!/usr/bin/env bash
#
# Regression tests for what HomeBrain is allowed to delete — locally
# (common.sh:prunable_archives) and off-site (common.sh:offsite_sync).
#
# The bugs this pins:
#   * offsite_sync used `rclone sync`, which mirrors deletions. Anything that
#     removed a local archive — a failed drive, ransomware, the emergency prune
#     in backup.sh — erased the off-site copy on the next run. The one event the
#     off-site copy exists for was also the event that destroyed it.
#   * the emergency prune deleted archives down to zero to free space for a
#     backup that had not been written yet, let alone verified.
#
# The off-site half uses rclone's `alias` backend pointed at a temp directory,
# so the round-trip runs with no network and no credentials. offsite_env is
# redefined after sourcing common.sh to supply that remote in place of
# sftp/webdav/s3.
#
#   bash scripts/tests/test_backup_retention.sh
#
# Linux-only: prunable_archives uses `find -printf` and `head -n -1`, matching
# the GNU coreutils the product already targets. Exit status: 0 if every case
# passes, 1 otherwise. Skips cleanly without rclone.

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
mkdir -p "$TMP/local" "$TMP/remote"

# Point the "offsite:" remote at a local directory.
offsite_env() {
    export RCLONE_CONFIG_OFFSITE_TYPE=alias
    export RCLONE_CONFIG_OFFSITE_REMOTE="$TMP/remote"
}

export BACKUP_MOUNTDIR="$TMP/local"
export OFFSITE_PATH="backups"
REMOTE="$TMP/remote/backups"

archive() { echo "payload-$1" > "$TMP/local/homebrain_backup_$1.tar.gz.gpg"; }

# ── Local prune guard (no rclone needed) ────────────────────────────────────

# prunable_archives needs GNU `find -printf`. Without it the helper returns
# nothing and every assertion below would pass vacuously — which is worse than
# skipping, because it reports green on a host that proved nothing.
if find "$TMP" -maxdepth 0 -printf '' 2>/dev/null; then
    HAVE_GNU_FIND=true
else
    HAVE_GNU_FIND=false
fi

echo "== the emergency prune never eats the last archive =="
# common.sh:prunable_archives — the guard is in what it refuses to return.
PR="$TMP/prune"; mkdir -p "$PR"
if [ "$HAVE_GNU_FIND" != true ]; then
    printf '  skip  prune guard (needs GNU find -printf)\n'
else
count_prunable() { prunable_archives "$PR" | grep -c . ; }
if [ "$(count_prunable)" -eq 0 ]; then ok "empty dir yields no candidates"; else bad "empty dir yields no candidates"; fi
: > "$PR/homebrain_backup_a.tar.gz.gpg"
if [ "$(count_prunable)" -eq 0 ]; then
    ok "one archive yields no candidates (the last one is never prunable)"
else
    bad "one archive yields no candidates (offered the only backup for deletion)"
fi
sleep 1; : > "$PR/homebrain_backup_b.tar.gz.gpg"
sleep 1; : > "$PR/homebrain_backup_c.tar.gz.gpg"
if [ "$(count_prunable)" -eq 2 ]; then ok "three archives yield two candidates"; else bad "three archives yield two candidates (got $(count_prunable))"; fi
if [ "$(prunable_archives "$PR" | head -n1)" = "$PR/homebrain_backup_a.tar.gz.gpg" ]; then
    ok "candidates are oldest-first"
else
    bad "candidates are oldest-first"
fi
if prunable_archives "$PR" | grep -q "homebrain_backup_c"; then
    bad "newest archive excluded from candidates (it was offered)"
else
    ok "newest archive excluded from candidates"
fi
fi

echo "== backup.sh consumes the guarded helper =="
if grep -q 'prunable_archives "\$BACKUP_MOUNTDIR"' "$SCRIPT_DIR/../backup.sh"; then
    ok "backup.sh prunes via prunable_archives"
else
    bad "backup.sh prunes via prunable_archives (rolled its own find again)"
fi

# ── Off-site copy semantics (needs rclone) ──────────────────────────────────

if ! command -v rclone >/dev/null 2>&1; then
    echo
    echo "rclone not installed — skipping the off-site half."
    echo "passed: $pass   failed: $fail"
    [ "$fail" -eq 0 ]
    exit $?
fi

echo "== the copy reaches the remote =="
archive 2026-07-01
archive 2026-07-02
if offsite_sync >/dev/null 2>&1; then
    ok "offsite_sync succeeds"
else
    bad "offsite_sync succeeds (returned non-zero)"
fi
if [ -f "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" ] &&
   [ -f "$REMOTE/homebrain_backup_2026-07-02.tar.gz.gpg" ]; then
    ok "both archives copied"
else
    bad "both archives copied (missing on remote)"
fi

echo "== a local wipe does NOT delete the remote copy =="
# The regression. Under `sync` the remote emptied out with the local drive.
rm -f "$TMP"/local/*.tar.gz.gpg
if offsite_sync >/dev/null 2>&1; then
    ok "offsite_sync succeeds after local wipe"
else
    bad "offsite_sync succeeds after local wipe (returned non-zero)"
fi
remaining=$(find "$REMOTE" -name '*.tar.gz.gpg' 2>/dev/null | wc -l | tr -d ' ')
if [ "$remaining" = "2" ]; then
    ok "remote still holds both archives after the local drive emptied"
else
    bad "remote still holds both archives (found $remaining, expected 2)"
fi

echo "== age-based retention prunes the remote =="
# Backdate one remote archive past the window; it should go, the other stay.
touch -d '200 days ago' "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" 2>/dev/null \
    || touch -A -2000000 "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" 2>/dev/null
archive 2026-07-03
OFFSITE_KEEP_DAYS=90 offsite_sync >/dev/null 2>&1
if [ ! -f "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" ]; then
    ok "archive older than OFFSITE_KEEP_DAYS pruned"
else
    bad "archive older than OFFSITE_KEEP_DAYS pruned (still present)"
fi
if [ -f "$REMOTE/homebrain_backup_2026-07-02.tar.gz.gpg" ]; then
    ok "archive inside the window kept"
else
    bad "archive inside the window kept (was deleted)"
fi

echo "== retention ignores files HomeBrain did not put there =="
echo "someone elses data" > "$REMOTE/not-ours.txt"
touch -d '200 days ago' "$REMOTE/not-ours.txt" 2>/dev/null \
    || touch -A -2000000 "$REMOTE/not-ours.txt" 2>/dev/null
OFFSITE_KEEP_DAYS=90 offsite_sync >/dev/null 2>&1
if [ -f "$REMOTE/not-ours.txt" ]; then
    ok "unrelated remote file untouched by retention"
else
    bad "unrelated remote file untouched by retention (deleted it)"
fi

echo "== the off-site copy can be listed and fetched back =="
# The gap this closes: rclone pushed and nothing pulled, so the one scenario
# off-site backups exist for (local drive dead) needed a shell.
listing="$(offsite_list 2>/dev/null)"
if echo "$listing" | grep -q 'homebrain_backup_2026-07-02.tar.gz.gpg'; then
    ok "offsite_list reports a remote archive"
else
    bad "offsite_list reports a remote archive (got: ${listing:0:120})"
fi
if echo "$listing" | grep -q 'not-ours.txt'; then
    bad "offsite_list excludes foreign files (it listed one)"
else
    ok "offsite_list excludes foreign files"
fi

# Local drive dead: wipe local, fetch one named archive back, compare content.
rm -f "$TMP"/local/*.tar.gz.gpg
if offsite_fetch "homebrain_backup_2026-07-02.tar.gz.gpg" "$TMP/local" >/dev/null 2>&1; then
    ok "offsite_fetch succeeds"
else
    bad "offsite_fetch succeeds (returned non-zero)"
fi
if [ "$(cat "$TMP/local/homebrain_backup_2026-07-02.tar.gz.gpg" 2>/dev/null)" = "payload-2026-07-02" ]; then
    ok "fetched archive is byte-identical to what was backed up"
else
    bad "fetched archive is byte-identical to what was backed up"
fi
# Exactly one archive, not the whole remote.
fetched=$(find "$TMP/local" -name '*.tar.gz.gpg' | wc -l | tr -d ' ')
if [ "$fetched" = "1" ]; then
    ok "fetch pulls only the named archive"
else
    bad "fetch pulls only the named archive (got $fetched)"
fi

if [ "$(offsite_size 'homebrain_backup_2026-07-02.tar.gz.gpg' 2>/dev/null)" = "19" ]; then
    ok "offsite_size reports the remote byte count"
else
    bad "offsite_size reports the remote byte count (got '$(offsite_size 'homebrain_backup_2026-07-02.tar.gz.gpg' 2>/dev/null)', expected 19)"
fi

echo "== restore.sh accepts --from-offsite in any argument order =="
if grep -q -- '--from-offsite' "$SCRIPT_DIR/../restore.sh"; then
    ok "restore.sh handles --from-offsite"
else
    bad "restore.sh handles --from-offsite (flag not found)"
fi
if grep -q 'ARG_FLAG' "$SCRIPT_DIR/../restore.sh"; then
    bad "restore.sh still matches --no-prompt positionally as \$2"
else
    ok "restore.sh parses flags positionally-independently"
fi

echo "== offsite_mirror serialises, so a resume can't race a running mirror =="
# homebrain-offsite.timer fires hourly and on boot. Without the lock, a firing
# during a multi-hour upload would start a second concurrent mirror.
export OFFSITE_LOCK_FILE="$TMP/offsite.lock"
export OFFSITE_STATE_FILE="$TMP/offsite.json"
if ( offsite_mirror ) >/dev/null 2>&1; then
    ok "offsite_mirror succeeds when the lock is free"
else
    bad "offsite_mirror succeeds when the lock is free"
fi
if grep -q '"ok": true' "$OFFSITE_STATE_FILE" 2>/dev/null; then
    ok "records success in the state file the health check reads"
else
    bad "records success in the state file the health check reads"
fi
# Hold the lock the way a long upload does, then fire again.
( exec 202>"$OFFSITE_LOCK_FILE"; flock -n 202 || exit 1; sleep 4 ) &
HOLDER=$!
sleep 1
if ( offsite_mirror ) 2>&1 | grep -q "already running"; then
    ok "a second mirror stands down while one holds the lock"
else
    bad "a second mirror stands down while one holds the lock"
fi
wait "$HOLDER" 2>/dev/null

echo "== offsite_sync no longer uses a deletion-mirroring sync =="
if grep -qE '^\s*rclone sync ' "$COMMON"; then
    bad "common.sh has an rclone sync again"
else
    ok "common.sh uses copy, not sync"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
