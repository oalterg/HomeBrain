#!/usr/bin/env bash
#
# Regression tests for common.sh:clear_partial_install — the fresh-install
# cleanup deploy.sh runs when .setup_complete is absent.
#
# The bug this pins: the cleanup wiped Nextcloud's data BIND MOUNT but left the
# nextcloud_html VOLUME, which holds config.php. A first deploy that installs
# Nextcloud and then fails for any reason (a slow box missing the
# wait_for_healthy budget is enough) leaves .setup_complete unwritten, so the
# RETRY deleted the data directory out from under an install that still
# believed it was installed. Every request then 503s with "Your data directory
# is invalid", and no retry can recover — each one wipes it again.
#
# Reproduced on an RPi4: one Nextcloud health-check timeout, and the box was
# permanently unbootable into Nextcloud.
#
# Second bug pinned: it ran `rm -rf` on the directory itself. Removing a
# bind-mount source out from under a running container severs the mount — the
# container keeps the deleted inode and never sees the recreated directory, so
# host-side repair (even writing .ncdata back) is invisible until the container
# is force-recreated. Verified live: creating .ncdata on the host changed
# nothing inside the container until `up -d --force-recreate`.
#
#   bash scripts/tests/test_fresh_install_wipe.sh
#
# Linux-only: uses `stat -c` and `find -delete`, matching the GNU coreutils the
# product already targets.

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

# Stub docker so the test never touches a real daemon; record the arguments so
# we can assert the volume half of the cleanup actually happens.
DOCKER_LOG="$TMP/docker-log"
: > "$DOCKER_LOG"
docker() { echo "$*" >> "$DOCKER_LOG"; return 0; }

# Quiet the logger; these assertions are about filesystem effects.
log_info() { :; }
log_warn() { :; }

export NEXTCLOUD_DATA_DIR="$TMP/nextcloud-data"
export VAULT_DATA_DIR="$TMP/vault-data"

seed() {
    rm -rf "$NEXTCLOUD_DATA_DIR" "$VAULT_DATA_DIR"
    mkdir -p "$NEXTCLOUD_DATA_DIR/admin/files" "$VAULT_DATA_DIR"
    # .ncdata is the dotfile whose absence produces "Your data directory is
    # invalid" — a glob-based wipe would miss it and leave the dir half-cleared.
    echo "# Nextcloud data directory" > "$NEXTCLOUD_DATA_DIR/.ncdata"
    echo "stale" > "$NEXTCLOUD_DATA_DIR/admin/files/old.txt"
    echo "stale" > "$VAULT_DATA_DIR/db.sqlite3"
}

echo "== the wipe clears both halves, not just the bind mount =="
seed
: > "$DOCKER_LOG"
clear_partial_install

if grep -q -- 'down -v' "$DOCKER_LOG"; then
    ok "named volumes are removed too (config.php cannot outlive the data dir)"
else
    bad "named volumes are removed too — no 'down -v' issued: $(cat "$DOCKER_LOG")"
fi
if grep -q -- '--remove-orphans' "$DOCKER_LOG"; then
    ok "orphaned containers from the failed attempt are removed"
else
    bad "orphaned containers from the failed attempt are removed"
fi

echo "== the bind mounts are emptied, dotfiles included =="
if [ -z "$(ls -A "$NEXTCLOUD_DATA_DIR" 2>/dev/null)" ]; then
    ok "nextcloud data dir is empty"
else
    bad "nextcloud data dir is empty (left: $(ls -A "$NEXTCLOUD_DATA_DIR" | tr '\n' ' '))"
fi
if [ ! -e "$NEXTCLOUD_DATA_DIR/.ncdata" ]; then
    ok ".ncdata removed (a glob-only wipe would have missed it)"
else
    bad ".ncdata removed"
fi
if [ -z "$(ls -A "$VAULT_DATA_DIR" 2>/dev/null)" ]; then
    ok "vault data dir is empty"
else
    bad "vault data dir is empty"
fi

echo "== the directories themselves survive, so live bind mounts stay valid =="
seed
before_nc=$(stat -c %i "$NEXTCLOUD_DATA_DIR")
before_vault=$(stat -c %i "$VAULT_DATA_DIR")
clear_partial_install
after_nc=$(stat -c %i "$NEXTCLOUD_DATA_DIR" 2>/dev/null || echo "GONE")
after_vault=$(stat -c %i "$VAULT_DATA_DIR" 2>/dev/null || echo "GONE")
if [ "$before_nc" = "$after_nc" ]; then
    ok "nextcloud data dir keeps its inode (mount not severed)"
else
    bad "nextcloud data dir keeps its inode ($before_nc -> $after_nc; a running container would be stranded on the deleted inode)"
fi
if [ "$before_vault" = "$after_vault" ]; then
    ok "vault data dir keeps its inode"
else
    bad "vault data dir keeps its inode ($before_vault -> $after_vault)"
fi

echo "== it is safe to run when there is nothing to clean =="
rm -rf "$NEXTCLOUD_DATA_DIR" "$VAULT_DATA_DIR"
if clear_partial_install >/dev/null 2>&1; then
    ok "no-op on a genuinely fresh box (missing dirs are not an error)"
else
    bad "no-op on a genuinely fresh box (returned non-zero)"
fi
mkdir -p "$NEXTCLOUD_DATA_DIR"
if clear_partial_install >/dev/null 2>&1 && [ -d "$NEXTCLOUD_DATA_DIR" ]; then
    ok "an already-empty dir is left in place"
else
    bad "an already-empty dir is left in place"
fi

echo "== deploy.sh no longer deletes the bind-mount directory itself =="
DEPLOY="$SCRIPT_DIR/../deploy.sh"
if grep -qE 'rm -rf -- "\$d"' "$DEPLOY"; then
    bad "deploy.sh still rm -rf's the bind-mount directory (severs live mounts)"
else
    ok "deploy.sh no longer rm -rf's the bind-mount directory"
fi
if grep -q 'clear_partial_install' "$DEPLOY"; then
    ok "deploy.sh delegates to the tested helper"
else
    bad "deploy.sh delegates to the tested helper"
fi

echo "== the wipe stays gated on .setup_complete =="
# The gate is what makes `down -v` safe: deploy.sh writes .setup_complete
# itself on success, so its absence means no deploy has ever completed and
# there is no claimed user data. If this gate ever goes, the cleanup becomes a
# data-loss bug on every redeploy of a working box.
if grep -qE '\[\[ ! -f "\$INSTALL_DIR/\.setup_complete" \]\]' "$DEPLOY"; then
    ok "cleanup runs only when .setup_complete is absent"
else
    bad "cleanup is no longer gated on .setup_complete — down -v would hit live installs"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
