#!/usr/bin/env bash
#
# Regression tests for what the setup wizard is told during a restore: when it
# may hand the credentials over, and where the restore's own log goes.
#
# The bug this pins, found on a bare-metal restore onto a fresh Pi: a restore
# install is deploy.sh AND THEN restore.sh, but deploy.sh promoted the staged
# credentials — and touched .setup_complete — the moment its own half finished.
# Refreshing the wizard during the (long) restore therefore landed on the
# handover screen, which offers "I have saved my password — log in": that calls
# cleanup_credentials, which deletes the in-flight marker and starts the tunnels
# while restore.sh is still writing the owner's data.
#
# Promotion now belongs to the end of the whole chain, and must still happen
# when the restore FAILS — the master password is shown exactly once, and a box
# whose restore died would otherwise have no way to ever show it.
#
# Same convention as test_instance_secrets.sh: the logic lives in common.sh and
# utilities.sh so it runs with no Docker, no network and no root.
#
#   bash scripts/tests/test_restore_handover.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# common.sh reads INSTALL_DIR and LOG_DIR from the environment, so point the
# whole test at a throwaway tree. chown root:root is not available unprivileged.
export INSTALL_DIR="$TMP/opt"
export LOG_DIR="$TMP/log"
mkdir -p "$INSTALL_DIR" "$LOG_DIR"
# The helper chowns the promoted file to root:root, which fails unprivileged
# and — under `set -e` — would end the script mid-promotion. Exported so the
# `bash utilities.sh` child below inherits the stub too.
chown() { :; }
export -f chown

# shellcheck source=../common.sh disable=SC1091
source "$SCRIPT_DIR/../common.sh" 2>/dev/null

STAGING="$INSTALL_DIR/.install_creds_staging"
FINAL="$INSTALL_DIR/install_creds.json"

reset_box() {
    rm -f "$STAGING" "$FINAL" "$INSTALL_DIR/.restoring" "$INSTALL_DIR/.restore_failed"
    echo '{"password":"secret"}' > "$STAGING"
}

echo "== promote_install_creds =="

reset_box
promote_install_creds
if [[ -f "$FINAL" && ! -f "$STAGING" ]]; then
    ok "staged credentials move to the path the wizard reads"
else
    bad "staged credentials move to the path the wizard reads"
fi

reset_box
rm -f "$STAGING"
promote_install_creds
if [[ ! -f "$FINAL" ]]; then
    ok "nothing to promote is not an error (dashboard restores stage nothing)"
else
    bad "nothing to promote is not an error"
fi

echo "== deploy.sh holds the handover back while a restore is pending =="

if grep -q 'if \[ -f "\$INSTALL_DIR/.restoring" \]' "$SCRIPT_DIR/../deploy.sh"; then
    ok "deploy.sh gates promotion on the in-flight marker"
else
    bad "deploy.sh gates promotion on the in-flight marker"
fi
if grep -q 'promote_install_creds' "$SCRIPT_DIR/../deploy.sh"; then
    ok "deploy.sh promotes through the shared helper"
else
    bad "deploy.sh promotes through the shared helper"
fi
if grep -q 'mv "\$INSTALL_DIR/.install_creds_staging"' "$SCRIPT_DIR/../deploy.sh"; then
    bad "deploy.sh no longer moves the staged file itself"
else
    ok "deploy.sh no longer moves the staged file itself"
fi

echo "== utilities.sh finish_restore =="

run_finish() {  # run_finish <rc>
    ( cd "$SCRIPT_DIR/.." && bash utilities.sh finish_restore "$1" ) >/dev/null 2>&1
}

reset_box
touch "$INSTALL_DIR/.restoring"
run_finish 0
if [[ -f "$FINAL" ]]; then
    ok "a finished restore hands the credentials over"
else
    bad "a finished restore hands the credentials over"
fi
if [[ ! -f "$INSTALL_DIR/.restoring" ]]; then
    ok "a finished restore clears the in-flight marker"
else
    bad "a finished restore clears the in-flight marker"
fi
if [[ ! -f "$INSTALL_DIR/.restore_failed" ]]; then
    ok "a finished restore is not flagged as failed"
else
    bad "a finished restore is not flagged as failed"
fi

reset_box
touch "$INSTALL_DIR/.restoring"
run_finish 1
if [[ -f "$FINAL" ]]; then
    ok "a FAILED restore still hands the credentials over (shown only once)"
else
    bad "a FAILED restore still hands the credentials over"
fi
if [[ ! -f "$INSTALL_DIR/.restoring" ]]; then
    ok "a FAILED restore clears the in-flight marker (no screen that never moves)"
else
    bad "a FAILED restore clears the in-flight marker"
fi
if [[ -f "$INSTALL_DIR/.restore_failed" ]]; then
    ok "a FAILED restore is flagged, so the screen cannot claim the files are back"
else
    bad "a FAILED restore is flagged"
fi

echo "== tunnels stay down until the credentials are claimed =="

# The guard existed but had never fired: it tested the promoted path, and
# during a first install the credentials are still at .install_creds_staging.
# So every first boot published its tunnels while it was still installing —
# on a restore, over a Nextcloud still carrying the BACKUP's trusted domains.
if grep -q '\.install_creds_staging' "$SCRIPT_DIR/../deploy.sh"; then
    ok "deploy.sh withholds tunnels for staged credentials, not only promoted ones"
else
    bad "deploy.sh withholds tunnels for staged credentials (the guard never fires otherwise)"
fi
if grep -q 'activate_tunnels' "$SCRIPT_DIR/../utilities.sh"; then
    ok "activate_tunnels exists to bring them up at handover"
else
    bad "activate_tunnels exists to bring them up at handover"
fi
# restore.sh restarts the stack itself, BEFORE it re-applies trusted domains.
# Guarding only deploy.sh moved the exposure later into the same restore
# instead of removing it — measured on hardware.
if grep -q '\.install_creds_staging' "$SCRIPT_DIR/../restore.sh"; then
    ok "restore.sh withholds tunnels too (its restart precedes the domain fix)"
else
    bad "restore.sh withholds tunnels too (its restart precedes the domain fix)"
fi

echo "== the factory password still works while a restore runs =="

# Holding the credentials back closed a door that was deliberately left open:
# login() keeps honouring the factory password until the owner has claimed
# their real one, and it asks is_handover_pending(). Reading only the promoted
# path would reject the device-label password for the whole restore, in favour
# of a master password the owner has never been shown.
if grep -q 'STAGING_CREDS_PATH' "$SCRIPT_DIR/../../src/app.py" \
   && sed -n '/^def is_handover_pending/,/^    return/p' "$SCRIPT_DIR/../../src/app.py" \
      | grep -q 'STAGING_CREDS_PATH'; then
    ok "is_handover_pending counts staged credentials, not only promoted ones"
else
    bad "is_handover_pending counts staged credentials (a lost session mid-restore cannot log back in)"
fi

echo "== restore.sh logs where its caller is looking =="

# The other half of the same defect: the wizard streams the setup log, but
# restore.sh re-opened its own stdout onto restore.log whenever it was not on a
# TTY. Redirecting the chain could not stop it, so the wizard's log froze at
# deploy.sh's last line for the whole restore and looked hung.
probe_restore_log() {  # probe_restore_log [override] -> prints the file written
    local override="${1:-}"
    rm -rf "$TMP/rl"; mkdir -p "$TMP/rl/log" "$TMP/rl/opt"
    (
        export INSTALL_DIR="$TMP/rl/opt" LOG_DIR="$TMP/rl/log"
        [[ -n "$override" ]] && export RESTORE_LOG_FILE="$override"
        # Fails immediately (no .env), which is enough: the redirect is the
        # first thing the script does, so the message lands in whichever file
        # it chose.
        bash "$SCRIPT_DIR/../restore.sh" "$TMP/rl/nope.tar.gz" --no-prompt
    ) >/dev/null 2>&1
    ls "$TMP/rl/log"
}

if [[ "$(probe_restore_log)" == "restore.log" ]]; then
    ok "unset: keeps its own restore.log (dashboard restores are unchanged)"
else
    bad "unset: keeps its own restore.log (got '$(probe_restore_log)')"
fi
if [[ "$(probe_restore_log "$TMP/rl/log/setup.log")" == "setup.log" ]]; then
    ok "set: writes to the caller's log, so the wizard keeps moving"
else
    bad "set: writes to the caller's log (got '$(probe_restore_log "$TMP/rl/log/setup.log")')"
fi

printf '\npassed: %d   failed: %d\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
