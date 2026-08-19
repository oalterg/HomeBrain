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
archive_system() { echo "sys-$1" > "$TMP/local/homebrain_backup_system_$1.tar.gz.gpg"; }

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

echo "== backup.sh publishes the archive by rename, after verifying it =="
BSH="$SCRIPT_DIR/../backup.sh"
if grep -q 'mv -f "\$ARCHIVE_TMP" "\$ARCHIVE_PATH"' "$BSH"; then
    ok "the finished archive is moved into its published name"
else
    bad "the finished archive is moved into its published name (rename gone)"
fi
if grep -qE -- '-o "\$ARCHIVE_PATH"|-czf "\$ARCHIVE_PATH"' "$BSH"; then
    bad "compression writes straight to the published name again (mirror can see a growing file)"
else
    ok "compression writes to the partial name, not the published one"
fi
# Verification must gate the rename, or a corrupt archive becomes visible —
# and mirrorable — before the check that deletes it has run.
v_line=$(grep -n 'Archive verified' "$BSH" | head -1 | cut -d: -f1)
m_line=$(grep -n 'mv -f "\$ARCHIVE_TMP"' "$BSH" | head -1 | cut -d: -f1)
if [ -n "$v_line" ] && [ -n "$m_line" ] && [ "$v_line" -lt "$m_line" ]; then
    ok "verification runs before the archive is published"
else
    bad "verification runs before the archive is published (order: verify=$v_line publish=$m_line)"
fi
if grep -q 'rm -f "\$BACKUP_MOUNTDIR"/\.homebrain_backup\*\.part' "$BSH"; then
    ok "leftover partials from a dead run are swept"
else
    bad "leftover partials from a dead run are swept (retention cannot see them, so nothing else will)"
fi

echo "== offsite_keep clamps to 1–8 and defaults to 3 =="
(
    unset OFFSITE_KEEP
    [ "$(offsite_keep)" = "3" ]
) && ok "unset OFFSITE_KEEP defaults to 3" || bad "unset OFFSITE_KEEP defaults to 3"
[ "$(OFFSITE_KEEP=1 offsite_keep)" = "1" ] && ok "OFFSITE_KEEP=1" || bad "OFFSITE_KEEP=1"
[ "$(OFFSITE_KEEP=8 offsite_keep)" = "8" ] && ok "OFFSITE_KEEP=8" || bad "OFFSITE_KEEP=8"
[ "$(OFFSITE_KEEP=0 offsite_keep)" = "3" ] && ok "OFFSITE_KEEP=0 falls back to 3" || bad "OFFSITE_KEEP=0 falls back to 3"
[ "$(OFFSITE_KEEP=9 offsite_keep)" = "3" ] && ok "OFFSITE_KEEP=9 falls back to 3" || bad "OFFSITE_KEEP=9 falls back to 3"
[ "$(OFFSITE_KEEP=foo offsite_keep)" = "3" ] && ok "junk OFFSITE_KEEP falls back to 3" || bad "junk OFFSITE_KEEP falls back to 3"

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
if offsite_sync >/dev/null 2>&1; then
    ok "offsite_sync succeeds"
else
    bad "offsite_sync succeeds (returned non-zero)"
fi
if [ -f "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" ]; then
    ok "archive copied"
else
    bad "archive copied (missing on remote)"
fi

echo "== a local wipe does NOT delete the remote copy =="
# The regression. Under `sync` the remote emptied out with the local drive.
rm -f "$TMP"/local/*.tar.gz.gpg
if offsite_sync >/dev/null 2>&1; then
    ok "offsite_sync succeeds after local wipe"
else
    bad "offsite_sync succeeds after local wipe (returned non-zero)"
fi
if [ -f "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" ]; then
    ok "remote still holds the archive after the local drive emptied"
else
    bad "remote still holds the archive after the local drive emptied"
fi

echo "== off-site keeps the newest OFFSITE_KEEP full backups =="
# Local retention keeps more than off-site (backup.sh: BACKUP_RETENTION).
# Off-site keep-N is smaller because a superseded multi-GB archive should
# not keep costing upload bandwidth on a home uplink. Default is 3; this
# case pins keep=2 so the prune is observable with three archives.
sleep 1; archive 2026-07-02
sleep 1; archive 2026-07-03
OFFSITE_KEEP=2 offsite_sync >/dev/null 2>&1
if [ -f "$REMOTE/homebrain_backup_2026-07-03.tar.gz.gpg" ] \
    && [ -f "$REMOTE/homebrain_backup_2026-07-02.tar.gz.gpg" ]; then
    ok "newest OFFSITE_KEEP full archives kept"
else
    bad "newest OFFSITE_KEEP full archives kept (missing 02 or 03)"
fi
if [ ! -f "$REMOTE/homebrain_backup_2026-07-01.tar.gz.gpg" ]; then
    ok "archives older than OFFSITE_KEEP pruned"
else
    bad "archives older than OFFSITE_KEEP pruned (01 is still on the remote)"
fi

echo "== a superseded archive still sitting locally is never re-uploaded =="
# The bug this guards: with local retention keeping extra copies and
# off-site keeping N, a naive blanket copy would re-upload everything
# older than N every hourly resume tick (dest missing it, source still
# has it) only to prune it straight back out — paying upload cost for a
# file that is deleted the instant it lands.
rm -f "$REMOTE"/homebrain_backup_2026-07-0[12].tar.gz.gpg 2>/dev/null
: > "$TMP/copy-log"
rclone_orig=$(command -v rclone)
rclone() { echo "$*" >> "$TMP/copy-log"; command "$rclone_orig" "$@"; }
OFFSITE_KEEP=1 offsite_sync >/dev/null 2>&1
unset -f rclone
if grep -q 'homebrain_backup_2026-07-02.tar.gz.gpg' "$TMP/copy-log"; then
    bad "superseded archive left on the local drive is not re-uploaded (it was)"
else
    ok "superseded archive left on the local drive is not re-uploaded"
fi

echo "== system snapshots stay local =="
# Pre-update snapshots are a local rollback point. They do not contain the
# user's files, so copying them off-site was a restore-wizard footgun and
# contradicted update.sh's --skip-offsite.
archive_system 2026-07-01
offsite_sync >/dev/null 2>&1
if [ ! -f "$REMOTE/homebrain_backup_system_2026-07-01.tar.gz.gpg" ]; then
    ok "system snapshot is not copied off-site"
else
    bad "system snapshot is not copied off-site (it landed on the remote)"
fi
if [ -f "$REMOTE/homebrain_backup_2026-07-03.tar.gz.gpg" ]; then
    ok "full-backup retention unaffected by skipping system snapshots"
else
    bad "full-backup retention unaffected by skipping system snapshots (newest full archive disappeared)"
fi

echo "== leftover remote system snapshots are removed once a full exists =="
# Older versions mirrored snapshots. A full archive already contains
# everything they do, so they can go — but only when a local full is
# present. Plant one as if an older mirror had put it there.
echo "sys-leftover" > "$REMOTE/homebrain_backup_system_old.tar.gz.gpg"
offsite_sync >/dev/null 2>&1
if [ ! -f "$REMOTE/homebrain_backup_system_old.tar.gz.gpg" ]; then
    ok "leftover remote system snapshot removed when a local full exists"
else
    bad "leftover remote system snapshot removed when a local full exists (still present)"
fi

echo "== retention ignores files HomeBrain did not put there =="
echo "someone elses data" > "$REMOTE/not-ours.txt"
touch -d '200 days ago' "$REMOTE/not-ours.txt" 2>/dev/null \
    || touch -A -2000000 "$REMOTE/not-ours.txt" 2>/dev/null
offsite_sync >/dev/null 2>&1
if [ -f "$REMOTE/not-ours.txt" ]; then
    ok "unrelated remote file untouched by retention"
else
    bad "unrelated remote file untouched by retention (deleted it)"
fi

echo "== the off-site copy can be listed and fetched back =="
# The gap this closes: rclone pushed and nothing pulled, so the one scenario
# off-site backups exist for (local drive dead) needed a shell.
listing="$(offsite_list 2>/dev/null)"
if echo "$listing" | grep -q 'homebrain_backup_2026-07-03.tar.gz.gpg'; then
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
if offsite_fetch "homebrain_backup_2026-07-03.tar.gz.gpg" "$TMP/local" >/dev/null 2>&1; then
    ok "offsite_fetch succeeds"
else
    bad "offsite_fetch succeeds (returned non-zero)"
fi
if [ "$(cat "$TMP/local/homebrain_backup_2026-07-03.tar.gz.gpg" 2>/dev/null)" = "payload-2026-07-03" ]; then
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

if [ "$(offsite_size 'homebrain_backup_2026-07-03.tar.gz.gpg' 2>/dev/null)" = "19" ]; then
    ok "offsite_size reports the remote byte count"
else
    bad "offsite_size reports the remote byte count (got '$(offsite_size 'homebrain_backup_2026-07-03.tar.gz.gpg' 2>/dev/null)', expected 19)"
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
# Redirected HERE, before the first offsite_mirror call, not further down where
# it is needed: offsite_mirror writes this path and rm -f's it on the way out,
# so without the override a test run on a live box deletes the run-file of a
# mirror that is genuinely in flight — the dashboard then shows "not syncing"
# and the health check warns, both about a mirror that is running fine.
export OFFSITE_RUN_FILE="$TMP/offsite.running"
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

# ── Retention edge cases (these reshape the remote, so they run last) ───────

echo "== legacy nextcloud_backup* archives are pruned like full backups =="
# backup.sh's local retention counts nextcloud_backup* in the same keep-N pool
# as homebrain_backup*, so off-site must treat it as a full backup too. An
# earlier revision copied it off-site but excluded it from every prune path,
# which grew the remote without bound.
rm -f "$TMP"/local/*.tar.gz.gpg "$REMOTE"/*.tar.gz.gpg
echo "payload-legacy" > "$TMP/local/nextcloud_backup_2026-06-01.tar.gz.gpg"
offsite_sync >/dev/null 2>&1
if [ -f "$REMOTE/nextcloud_backup_2026-06-01.tar.gz.gpg" ]; then
    ok "legacy archive copied off-site"
else
    bad "legacy archive copied off-site (missing on remote)"
fi
sleep 1; echo "payload-2026-07-10" > "$TMP/local/homebrain_backup_2026-07-10.tar.gz.gpg"
OFFSITE_KEEP=1 offsite_sync >/dev/null 2>&1
if [ ! -f "$REMOTE/nextcloud_backup_2026-06-01.tar.gz.gpg" ]; then
    ok "superseded legacy archive pruned (not left to grow forever)"
else
    bad "superseded legacy archive pruned (still on the remote)"
fi

echo "== a dead local drive never triggers a remote prune =="
# The copy-not-sync rule exists so a local failure cannot propagate into a
# remote deletion. Retention must not sneak that back in: with no local full
# archive the newest remote one is the only copy of the user's data left, and
# the older ones beside it are the only redundancy. Leftover system snapshots
# stay too — they may be the only copy of vault/config left anywhere.
cp "$TMP/local/homebrain_backup_2026-07-10.tar.gz.gpg" \
   "$REMOTE/homebrain_backup_2026-07-09.tar.gz.gpg"
echo "sys-stranded" > "$REMOTE/homebrain_backup_system_stranded.tar.gz.gpg"
before=$(find "$REMOTE" -name 'homebrain_backup_*.tar.gz.gpg' | wc -l | tr -d ' ')
rm -f "$TMP"/local/*.tar.gz.gpg          # the drive dies
offsite_sync >/dev/null 2>&1
after=$(find "$REMOTE" -name 'homebrain_backup_*.tar.gz.gpg' | wc -l | tr -d ' ')
if [ "$before" = "$after" ] && [ "$after" -gt 1 ]; then
    ok "no local full archive means no remote prune (kept $after)"
else
    bad "no local full archive means no remote prune (went $before -> $after)"
fi
if [ -f "$REMOTE/homebrain_backup_system_stranded.tar.gz.gpg" ]; then
    ok "no local full archive means leftover system snapshots stay"
else
    bad "no local full archive means leftover system snapshots stay (deleted the only copy)"
fi

echo "== the mirror publishes a run-file readers can poll without interfering =="
# The dashboard's status line must never probe the mirror's LOCK: taking it,
# even briefly, makes a mirror starting in that window see it held, log
# "already running" and return success — displaying status would silently skip
# an off-site copy. The mirror publishes its PID instead; readers only read.
export OFFSITE_RUN_FILE="$TMP/offsite.running"
rm -f "$OFFSITE_LOCK_FILE"
if grep -q 'OFFSITE_RUN_FILE' "$COMMON"; then
    ok "common.sh publishes a run-file"
else
    bad "common.sh publishes a run-file"
fi
( offsite_mirror ) >/dev/null 2>&1
if [ ! -f "$OFFSITE_RUN_FILE" ]; then
    ok "run-file cleared once the mirror finishes"
else
    bad "run-file cleared once the mirror finishes (left behind -> phantom 'syncing')"
fi
if grep -q 'homebrain-offsite.lock' "$SCRIPT_DIR/../../src/app.py" 2>/dev/null; then
    bad "app.py still probes the mirror's lock file (can cancel a mirror)"
else
    ok "app.py does not touch the mirror's lock file"
fi

echo "== a long upload reports progress instead of going silent for hours =="
# rclone logs transfer stats at INFO while its own default log level is
# NOTICE, so a plain `rclone copy` of an 80 GiB archive prints nothing at all
# between "Mirroring backups off-site..." and "Off-site mirror complete" —
# hours in which a slow link and a wedged one look identical, and nothing says
# WHICH archive is moving. System snapshots are not copied; the full-archive
# copy is the one that must not go silent.
rm -f "$TMP"/local/*.tar.gz.gpg "$REMOTE"/*.tar.gz.gpg
echo "payload-progress" > "$TMP/local/homebrain_backup_2026-07-20.tar.gz.gpg"
archive_system 2026-07-20
: > "$TMP/copy-log"
rclone_orig=$(command -v rclone)
rclone() { echo "$*" >> "$TMP/copy-log"; command "$rclone_orig" "$@"; }
offsite_sync >/dev/null 2>&1
unset -f rclone
copies=$(grep -c '^copy ' "$TMP/copy-log" || true)
staty=$(grep '^copy ' "$TMP/copy-log" \
    | grep -c -- '--stats [0-9]\+m --stats-one-line --stats-log-level NOTICE' || true)
if [ "${copies:-0}" -eq 1 ] && [ "${staty:-0}" -eq 1 ]; then
    ok "the full-archive copy asks for periodic progress"
else
    bad "the full-archive copy asks for periodic progress (copies=${copies:-0} stats=${staty:-0})"
fi
if grep '^copy ' "$TMP/copy-log" | grep -q 'homebrain_backup_system_'; then
    bad "system snapshots are not among the copy includes (one was)"
else
    ok "system snapshots are not among the copy includes"
fi

echo "== an archive still being written is invisible to the mirror =="
# The failure this pins, seen live on 2026-08-03: an 89 GB full takes ~30
# minutes to write, homebrain-offsite.timer fires hourly, so the mirror landed
# mid-write, picked the growing file as the newest full, and rclone refused it
# ("can't copy - source file is being updated"). That recorded an off-site
# failure and pushed an alert for a backup that was fine — and, worse, the
# newest FINISHED archive was skipped, because the partial outranked it.
rm -f "$TMP"/local/*.tar.gz.gpg "$REMOTE"/*.tar.gz.gpg
echo "payload-finished" > "$TMP/local/homebrain_backup_2026-08-01.tar.gz.gpg"
sleep 1
# Newer than the finished one — the ordering that made this bite.
echo "still-growing" > "$TMP/local/.homebrain_backup_2026-08-02.tar.gz.gpg.part"
offsite_sync >/dev/null 2>&1
if [ -f "$REMOTE/homebrain_backup_2026-08-01.tar.gz.gpg" ]; then
    ok "the newest finished archive is mirrored even with a partial beside it"
else
    bad "the newest finished archive is mirrored even with a partial beside it (partial won 'newest')"
fi
if find "$REMOTE" -name '*.part' 2>/dev/null | grep -q .; then
    bad "a partial archive is never uploaded (it was)"
else
    ok "a partial archive is never uploaded"
fi
if [ "$HAVE_GNU_FIND" = true ]; then
    if prunable_archives "$TMP/local" | grep -q '\.part'; then
        bad "local retention ignores partials (offered one for deletion)"
    else
        ok "local retention ignores partials"
    fi
fi
rm -f "$TMP"/local/.homebrain_backup*.part

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
