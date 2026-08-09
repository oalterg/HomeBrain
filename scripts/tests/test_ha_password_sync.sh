#!/usr/bin/env bash
#
# Regression tests for common.sh:ha_sync_admin_password — the one place that
# decides whether Home Assistant really answers to the box's master password.
#
# The bugs these pin:
#   * `hass --script auth` exits 0 when it changed nothing (#145). Anything
#     gating on that exit code records a password HA has never accepted.
#   * restore.sh put an archive's auth store back and never re-asserted the
#     current password, so a restored box came up with the dashboard and
#     Nextcloud on the master password and Home Assistant on an older one —
#     which, after a bare-metal restore, nobody has.
#
# docker/curl/jq are shell functions here: shell functions win over PATH, and a
# real HA cannot be made to refuse a password on demand.
#
#   bash scripts/tests/test_ha_password_sync.sh
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

# --- the fake Home Assistant ------------------------------------------------

RUNNING=true        # docker inspect .State.Running
CLI_RC=0            # `hass --script auth change_password` exit status
LOGIN='{"type": "create_entry"}'
OWNER=admin         # what the auth store says owns Home Assistant
CHANGED_USER=""     # which username change_password was actually aimed at
RECORDED=""         # what was written back to .env

# The recorded ownership these cases run under. Set explicitly rather than
# left to the migration path, so each case exercises the branch it names.
HA_ADMIN_USER=admin
HA_PASSWORD_MANAGED=true
HA_ADMIN_PASSWORD=onrecord

update_env_var() { RECORDED="${RECORDED}${1}=${2} "; }
harden_env_file() { :; }

get_ha_cid() { echo "ha-test-cid"; }

docker() {
    case "$1" in
        inspect) echo "$RUNNING" ;;
        exec)
            # `docker exec <cid> python3 -c ...` is the owner lookup;
            # `docker exec <cid> hass --script auth ...` is the change.
            case "$3" in
                python3) echo "$OWNER"; return 0 ;;
                hass)    CHANGED_USER="${*: -2:1}"; return "$CLI_RC" ;;
            esac
            return "$CLI_RC" ;;
        restart) return 0 ;;
        *)       return 0 ;;
    esac
}

curl() {
    local url="${*: -1}"
    case "$url" in
        */api/onboarding)   echo 200 ;;
        */auth/login_flow)  echo '{"flow_id":"F1"}' ;;
        */auth/login_flow/*) echo "$LOGIN" ;;
    esac
}

jq() {
    case "$1" in
        -r)  cat >/dev/null; echo "F1" ;;
        -nc) echo '{"client_id":"c","username":"admin","password":"p"}' ;;
    esac
}

sleep() { :; }

echo "== the exit code is not the answer =="

# The #145 trap: the CLI reports success, HA still refuses the password.
CLI_RC=0
LOGIN='{"errors":{"base":"invalid_auth"}}'
if ha_sync_admin_password "hunter2" >/dev/null 2>&1; then
    bad "reports failure when HA refuses the password the CLI 'set'"
else
    ok "reports failure when HA refuses the password the CLI 'set'"
fi

LOGIN='{"type": "create_entry"}'
if ha_sync_admin_password "hunter2" >/dev/null 2>&1; then
    ok "reports success only when a login actually works"
else
    bad "reports success only when a login actually works (said no)"
fi

echo "== the owner is not always called admin =="

# A box provisioned before HA_ADMIN_USER existed has no record to read, so the
# account is discovered once and written down. Live on the production box:
# Home Assistant migrated from an older system, owner account
# `oliaidanaberlin`, no user named `admin` at all. Aiming at `admin` there
# edits nobody, and the CLI still exits 0.
unrecorded() { HA_ADMIN_USER=""; HA_PASSWORD_MANAGED=""; RECORDED=""; CHANGED_USER=""; }

unrecorded
OWNER=oliaidanaberlin
LOGIN='{"type": "create_entry"}'
ha_sync_admin_password "hunter2" >/dev/null 2>&1
if [ "$CHANGED_USER" = "oliaidanaberlin" ]; then
    ok "changes the password of the account that owns HA"
else
    bad "changes the password of the account that owns HA (aimed at '${CHANGED_USER:-nothing}')"
fi

# Refusing beats guessing: `admin` is a real account on most boxes, so a
# fallback would quietly rotate the wrong user's password on the boxes where
# the lookup failed. Code 2 is "nothing was attempted" — the callers must not
# report that as a rejected password.
unrecorded
OWNER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
if [ "$?" -eq 2 ]; then
    ok "refuses rather than guessing when the owner cannot be read"
else
    bad "refuses rather than guessing when the owner cannot be read"
fi
OWNER=admin

echo "== a password HomeBrain never set is not HomeBrain's to change =="

# Home Assistant lets its owner change their own password, and on a migrated
# box the account predates HomeBrain entirely. Rotating it there would take
# away the login they have been using.
HA_ADMIN_USER=admin
HA_PASSWORD_MANAGED=false
CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 3 ] && [ -z "$CHANGED_USER" ]; then
    ok "leaves a self-managed password alone, and says so distinctly (3)"
else
    bad "leaves a self-managed password alone (rc=$rc, aimed at '${CHANGED_USER:-nothing}')"
fi

# The migration decides ownership by *proof*, not by the account's name:
# HA either already accepts the password on record, or it does not.
unrecorded
LOGIN='{"type": "create_entry"}'
ha_sync_admin_password "hunter2" >/dev/null 2>&1
case "$RECORDED" in
    *"HA_PASSWORD_MANAGED=true"*) ok "records a box whose HA accepts the recorded password as managed" ;;
    *) bad "records a box whose HA accepts the recorded password as managed (got '$RECORDED')" ;;
esac

unrecorded
LOGIN='{"errors":{"base":"invalid_auth"}}'
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 3 ] && [ -z "$CHANGED_USER" ]; then
    case "$RECORDED" in
        *"HA_PASSWORD_MANAGED=false"*) ok "records a box whose HA refuses it as self-managed, and changes nothing" ;;
        *) bad "records a box whose HA refuses it as self-managed (got '$RECORDED')" ;;
    esac
else
    bad "a box whose HA refuses the recorded password must not be rotated (rc=$rc)"
fi

echo "== taking ownership is a separate, deliberate act =="

# The dashboard's "Let HomeBrain manage it". The one path that may overwrite a
# self-managed password — because the owner asked for it by name.
HA_ADMIN_USER=admin
HA_PASSWORD_MANAGED=false
RECORDED=""
CHANGED_USER=""
OWNER=oliaidanaberlin
LOGIN='{"type": "create_entry"}'
if ha_adopt_admin_password "newmaster" >/dev/null 2>&1; then
    ok "adopting sets the password even on a self-managed account"
else
    bad "adopting sets the password even on a self-managed account"
fi
if [ "$CHANGED_USER" = "oliaidanaberlin" ]; then
    ok "adopting aims at the account that owns HA"
else
    bad "adopting aims at the account that owns HA (aimed at '${CHANGED_USER:-nothing}')"
fi
case "$RECORDED" in
    *"HA_PASSWORD_MANAGED=true"*) ok "adopting records that HomeBrain manages it from now on" ;;
    *) bad "adopting records that HomeBrain manages it from now on (got '$RECORDED')" ;;
esac
case "$RECORDED" in
    *"HA_ADMIN_PASSWORD=newmaster"*) ok "adopting records the password it just set" ;;
    *) bad "adopting records the password it just set (got '$RECORDED')" ;;
esac

# A refused change must not leave .env claiming a password HA never took —
# that is #145 with an extra step.
RECORDED=""
LOGIN='{"errors":{"base":"invalid_auth"}}'
if ha_adopt_admin_password "newmaster" >/dev/null 2>&1; then
    bad "adopting reports failure when HA refuses the new password"
else
    ok "adopting reports failure when HA refuses the new password"
fi
case "$RECORDED" in
    *HA_*) bad "a refused adoption must record nothing (recorded '$RECORDED')" ;;
    *) ok "a refused adoption records nothing" ;;
esac

OWNER=admin
HA_ADMIN_USER=admin
HA_PASSWORD_MANAGED=true
LOGIN='{"type": "create_entry"}'

echo "== nothing to talk to =="

CLI_RC=1
if ha_sync_admin_password "hunter2" >/dev/null 2>&1; then
    bad "reports failure when the auth CLI itself fails"
else
    ok "reports failure when the auth CLI itself fails"
fi
CLI_RC=0

RUNNING=false
if ha_sync_admin_password "hunter2" >/dev/null 2>&1; then
    bad "reports failure when the container is not running"
else
    ok "reports failure when the container is not running"
fi
RUNNING=true

get_ha_cid() { echo ""; }
if ha_sync_admin_password "hunter2" >/dev/null 2>&1; then
    bad "reports failure when there is no HA container at all"
else
    ok "reports failure when there is no HA container at all"
fi
get_ha_cid() { echo "ha-test-cid"; }

echo "== both callers use the shared helper =="

# Structural: a restore that does not re-assert the password is the bug, and a
# second private copy of this logic is how the CLI trap survived the first fix.
for script in restore.sh rotate_master_password.sh; do
    if grep -q 'ha_sync_admin_password' "$SCRIPT_DIR/../$script"; then
        ok "$script syncs the HA password through the shared helper"
    else
        bad "$script syncs the HA password through the shared helper (not found)"
    fi
    if grep -q 'change_password admin' "$SCRIPT_DIR/../$script"; then
        bad "$script has its own copy of the auth CLI call again"
    else
        ok "$script has no private copy of the auth CLI call"
    fi
    # "Nothing was attempted" read as "your password was rejected" is what sent
    # the owner to fix a password that was fine. Both callers must branch on
    # the code, not on success/failure.
    if grep -q '^\s*3)' "$SCRIPT_DIR/../$script"; then
        ok "$script says something different when the password is not ours"
    else
        bad "$script says something different when the password is not ours (no case for 3)"
    fi
done

# Ownership is recorded where it is known for certain — at creation — so that
# nothing downstream has to infer it from the account's name.
if grep -q 'ha_record_account "admin" "true"' "$SCRIPT_DIR/../utilities.sh"; then
    ok "create_ha_admin records the account it just created as managed"
else
    bad "create_ha_admin records the account it just created as managed (not found)"
fi

echo "== a caller under 'set -e' survives every outcome =="

# restore.sh and utilities.sh both run `set -euo pipefail`. A bare
# `ha_sync_admin_password "$pw"` followed by `case "$?"` ends the script on the
# spot for any non-zero code — so a box whose Home Assistant manages its own
# password (a perfectly ordinary answer) would abort the restore right after
# the data went back on disk. The callers must use `|| rc=$?`.
for script in restore.sh rotate_master_password.sh utilities.sh; do
    if grep -qE '(ha_sync_admin_password|ha_adopt_admin_password) "[^"]*" \|\| [a-z_]*rc=\$\?' \
        "$SCRIPT_DIR/../$script"; then
        ok "$script captures the code instead of letting set -e swallow it"
    else
        bad "$script captures the code instead of letting set -e swallow it"
    fi
done

# The real thing: run it the way restore.sh does, under the same flags.
for code_case in "3:self-managed" "2:unreadable owner" "1:refused"; do
    rc_want="${code_case%%:*}"; what="${code_case#*:}"
    case "$rc_want" in
        3) HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=false ;;
        2) unrecorded; OWNER="" ;;
        1) HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=true; CLI_RC=1 ;;
    esac
    if ( set -euo pipefail
         rc=0
         ha_sync_admin_password "hunter2" >/dev/null 2>&1 || rc=$?
         [ "$rc" -eq "$rc_want" ] ) 2>/dev/null; then
        ok "set -e caller reaches its own error handling for: $what"
    else
        bad "set -e caller reaches its own error handling for: $what"
    fi
    CLI_RC=0; OWNER=admin
done
HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=true

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
