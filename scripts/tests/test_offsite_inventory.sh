#!/usr/bin/env bash
#
# Tests for the off-site inventory the Backup page shows — how many archives
# are actually sitting on the remote (common.sh:offsite_inventory) and how that
# count is recorded across runs (common.sh:offsite_state_write).
#
# Why the count is taken here at all: /api/backup/offsite/status is polled
# while the Backup page is open, and it is meant to cost a file read. Listing
# the remote from there would put a WAN round trip behind every poll, so a
# mirror run — the only moment the answer changes — records it instead.
#
# The bug this pins: a failed mirror usually means the remote is unreachable,
# so the inventory call fails with it. Writing the state file unconditionally
# would then blank out the counts at exactly the moment the owner is looking at
# the page wondering what is still safe off-site. The previous inventory has to
# survive a failed run.
#
# Uses rclone's `alias` backend pointed at a temp directory, so this runs with
# no network and no credentials — same approach as test_backup_retention.sh.
#
#   bash scripts/tests/test_offsite_inventory.sh
#
# Exit status: 0 if every case passes, 1 otherwise. Skips cleanly without
# rclone or jq.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

for dep in rclone jq; do
    command -v "$dep" >/dev/null || { echo "SKIP: $dep is not installed."; exit 0; }
done

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REMOTE="$TMP/remote/backups"
mkdir -p "$REMOTE"

# Point the "offsite:" remote at a local directory.
offsite_env() {
    export RCLONE_CONFIG_OFFSITE_TYPE=alias
    export RCLONE_CONFIG_OFFSITE_REMOTE="$TMP/remote"
}

export OFFSITE_PATH="backups"
OFFSITE_STATE_FILE="$TMP/offsite.json"

# Sizes are what the inventory sums, so give the files distinct known ones.
remote_full()     { head -c "$2" /dev/zero > "$REMOTE/homebrain_backup_$1.tar.gz.gpg"; }
remote_snapshot() { head -c "$2" /dev/zero > "$REMOTE/homebrain_backup_system_$1.tar.gz.gpg"; }

# --- inventory ------------------------------------------------------------

remote_full     "2026-08-12_16-59-42" 4000
remote_snapshot "2026-08-09_11-42-25" 100
remote_snapshot "2026-08-09_12-07-55" 200

inv=$(offsite_inventory)
[[ "$(jq -r '.fulls'     <<<"$inv")" == "1" ]] && ok "counts the full archives"     || bad "counts the full archives (got: $inv)"
[[ "$(jq -r '.snapshots' <<<"$inv")" == "2" ]] && ok "counts the system snapshots" || bad "counts the system snapshots (got: $inv)"
[[ "$(jq -r '.bytes'     <<<"$inv")" == "4300" ]] && ok "sums the bytes on the remote" || bad "sums the bytes on the remote (got: $inv)"

# A snapshot is a homebrain_backup* too, so a naive filter counts it twice —
# once as a full. That would tell the owner they have a full archive off-site
# when they have none, which is the one thing this display must never do.
rm -f "$REMOTE"/homebrain_backup_2026-08-12_16-59-42.tar.gz.gpg
inv=$(offsite_inventory)
[[ "$(jq -r '.fulls' <<<"$inv")" == "0" ]] \
    && ok "snapshots are not counted as full archives" \
    || bad "snapshots are not counted as full archives (got: $inv)"

# --- state file -----------------------------------------------------------

offsite_state_write true
[[ "$(jq -r '.ok' "$OFFSITE_STATE_FILE")" == "true" ]] \
    && ok "records a successful mirror" || bad "records a successful mirror"
[[ "$(jq -r '.inventory.snapshots' "$OFFSITE_STATE_FILE")" == "2" ]] \
    && ok "records the inventory alongside the outcome" || bad "records the inventory alongside the outcome"
[[ "$(jq -r '.ts' "$OFFSITE_STATE_FILE")" =~ ^[0-9]+$ ]] \
    && ok "records a timestamp" || bad "records a timestamp"

# The regression: an unreachable remote fails the listing too. The last known
# inventory must survive rather than be overwritten with nothing.
offsite_list() { return 1; }
offsite_state_write false
[[ "$(jq -r '.ok' "$OFFSITE_STATE_FILE")" == "false" ]] \
    && ok "records a failed mirror" || bad "records a failed mirror"
[[ "$(jq -r '.inventory.snapshots' "$OFFSITE_STATE_FILE")" == "2" ]] \
    && ok "keeps the last known inventory through a failed mirror" \
    || bad "keeps the last known inventory through a failed mirror (got: $(cat "$OFFSITE_STATE_FILE"))"

# With no prior state and no reachable remote there is simply nothing to show —
# and it must still be valid JSON, because the dashboard parses it.
rm -f "$OFFSITE_STATE_FILE"
offsite_state_write false
jq -e '.inventory == null' "$OFFSITE_STATE_FILE" >/dev/null 2>&1 \
    && ok "writes valid JSON when the inventory is unknown" \
    || bad "writes valid JSON when the inventory is unknown (got: $(cat "$OFFSITE_STATE_FILE"))"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
