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
# away the login they have been using. HomeBrain's onboarded `admin` is not
# that case — the master password owns it.
HA_ADMIN_USER=oliaidanaberlin
HA_PASSWORD_MANAGED=false
OWNER=oliaidanaberlin
CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 3 ] && [ -z "$CHANGED_USER" ]; then
    ok "leaves a self-managed password alone, and says so distinctly (3)"
else
    bad "leaves a self-managed password alone (rc=$rc, aimed at '${CHANGED_USER:-nothing}')"
fi
OWNER=admin
HA_ADMIN_USER=admin
HA_PASSWORD_MANAGED=true

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
OWNER=oliaidanaberlin
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
OWNER=admin

# Restore / rotation: HomeBrain's onboarded `admin` still owns the store, but
# the recorded password is the new master and the store still has the old one.
# That must not file the box as self-managed — the admin login follows the
# master password.
unrecorded
LOGIN='{"type": "create_entry"}'
OWNER=admin
RECORDED=""; CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$CHANGED_USER" = "admin" ] && [ "$rc" -eq 0 ]; then
    case "$RECORDED" in
        *"HA_PASSWORD_MANAGED=true"*) ok "HomeBrain's admin account follows the master password even when the store still has the old one" ;;
        *) bad "HomeBrain's admin account follows the master password (got '$RECORDED')" ;;
    esac
else
    bad "HomeBrain's admin account follows the master password (rc=$rc, aimed at '${CHANGED_USER:-nothing}')"
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

echo "== a record that no longer describes this auth store is discarded =="

# The wizard's off-site restore deploys as a fresh install *first*: that runs
# create_ha_admin, which makes `admin` and records it in the regenerated .env.
# restore.sh then lays the archive's auth store on top, owned by whoever owned
# the box the archive came from. Believing the record there aims the change at
# a user who no longer exists — and the restored box keeps the archive's old
# Home Assistant password, which is the whole reason restore.sh syncs it.
HA_ADMIN_USER=admin          # what deploy.sh just recorded
HA_PASSWORD_MANAGED=true
OWNER=oliaidanaberlin        # what the restored auth store actually says
HA_ADMIN_PASSWORD=onrecord
LOGIN='{"type": "create_entry"}'
RECORDED=""; CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
if [ "$CHANGED_USER" = "oliaidanaberlin" ]; then
    ok "a restored box aims at the account that owns the restored store"
else
    bad "a restored box aims at the account that owns the restored store (aimed at '${CHANGED_USER:-nothing}')"
fi
case "$RECORDED" in
    *"HA_ADMIN_USER=oliaidanaberlin"*) ok "the stale record is replaced with the real owner" ;;
    *) bad "the stale record is replaced with the real owner (got '$RECORDED')" ;;
esac

# Ownership must be re-proved for the new account, not inherited. Here the
# restored store refuses the recorded password, so managed must flip to false
# even though the stale record said true.
OWNER=someone-else
LOGIN='{"errors":{"base":"invalid_auth"}}'
HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=true
RECORDED=""; CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 3 ] && [ -z "$CHANGED_USER" ]; then
    case "$RECORDED" in
        *"HA_PASSWORD_MANAGED=false"*) ok "ownership is re-proved for the new account, not inherited" ;;
        *) bad "ownership is re-proved for the new account (got '$RECORDED')" ;;
    esac
else
    bad "ownership is re-proved for the new account (rc=$rc, aimed at '${CHANGED_USER:-nothing}')"
fi

# A record that still matches must not be re-proved — that is the whole point
# of writing it down, and re-probing would undo a deliberate adoption.
OWNER=admin
HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=true
LOGIN='{"errors":{"base":"invalid_auth"}}'   # would prove "false" if re-probed
RECORDED=""; CHANGED_USER=""
ha_sync_admin_password "hunter2" >/dev/null 2>&1
if [ "$CHANGED_USER" = "admin" ] && [ -z "$RECORDED" ]; then
    ok "a record that still matches is believed, not re-proved"
else
    bad "a record that still matches is believed (aimed at '${CHANGED_USER:-nothing}', recorded '$RECORDED')"
fi
LOGIN='{"type": "create_entry"}'

echo "== ownership is not decided while HA is still starting =="

# restore.sh reaches the migration right after wait_for_healthy, which returns
# before the auth API answers — this file's own ha_set_password says so. A
# probe in that window refuses every password, which would file a
# HomeBrain-owned box as self-managed and permanently end the password sync
# that restore exists to perform.
# The countdown lives in a file: common.sh calls curl inside `$(...)`, so a
# shell variable would be decremented in a subshell and never seen here.
READY_AFTER=$(mktemp)
curl() {
    local url="${*: -1}" n
    n=$(cat "$READY_AFTER")
    case "$url" in
        */api/onboarding)
            if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$READY_AFTER"; echo 000
            else echo 200; fi ;;
        */auth/login_flow)   [ "$n" -gt 0 ] && echo "" || echo '{"flow_id":"F1"}' ;;
        */auth/login_flow/*) [ "$n" -gt 0 ] && echo "" || echo "$LOGIN" ;;
    esac
}
jq() { case "$1" in
           -r)  cat >/dev/null; [ "$(cat "$READY_AFTER")" -gt 0 ] && echo "" || echo "F1" ;;
           -nc) echo '{"client_id":"c","username":"u","password":"p"}' ;;
       esac; }
echo 3 > "$READY_AFTER"   # refuses three times, then the auth API is up

unrecorded
LOGIN='{"type": "create_entry"}'
ha_sync_admin_password "hunter2" >/dev/null 2>&1
case "$RECORDED" in
    *"HA_PASSWORD_MANAGED=true"*) ok "waits for the auth API before deciding who owns the password" ;;
    *) bad "waits for the auth API before deciding who owns the password (got '$RECORDED')" ;;
esac

# Never coming up must not be recorded as an answer at all.
echo 999 > "$READY_AFTER"
unrecorded
ha_sync_admin_password "hunter2" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ -z "$RECORDED" ]; then
    ok "records nothing when the auth API never answers"
else
    bad "records nothing when the auth API never answers (rc=$rc, recorded '$RECORDED')"
fi

# Restore the plain fakes for the remaining cases.
rm -f "$READY_AFTER"
curl() {
    local url="${*: -1}"
    case "$url" in
        */api/onboarding)   echo 200 ;;
        */auth/login_flow)  echo '{"flow_id":"F1"}' ;;
        */auth/login_flow/*) echo "$LOGIN" ;;
    esac
}
jq() { case "$1" in -r) cat >/dev/null; echo "F1" ;;
                    -nc) echo '{"client_id":"c","username":"u","password":"p"}' ;; esac; }
HA_ADMIN_USER=admin; HA_PASSWORD_MANAGED=true

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
        3) HA_ADMIN_USER=oliaidanaberlin; HA_PASSWORD_MANAGED=false; OWNER=oliaidanaberlin ;;
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

echo "== what a fresh provision creates, and what it writes down =="

# deploy.sh runs `utilities.sh ha_admin "$MASTER_PASSWORD"` on every deploy, so
# a new box always gets the account HomeBrain onboards for it. The name is a
# literal in the onboarding request; pin it, because everything downstream
# treats `admin` as HomeBrain's own account rather than someone's choice.
if grep -q '\\"username\\": \\"admin\\"' "$SCRIPT_DIR/../utilities.sh"; then
    ok "a fresh provision onboards Home Assistant as 'admin'"
else
    bad "a fresh provision onboards Home Assistant as 'admin' (not found)"
fi

# ...but only when it really created it. create_ha_admin returns early on a box
# whose Home Assistant was already onboarded — someone else's account, which
# HomeBrain must not claim to manage. Recording "admin/true" there would assert
# ownership of a stranger's login and let a later rotation overwrite it.
#
# Checked by line order rather than by calling it: utilities.sh must not be
# sourced from a test. It resolves SCRIPT_DIR from "$0", so sourcing it here
# makes it load scripts/tests/common.sh — and its `set -euo pipefail` then
# kills the sourcing shell when that fails, taking the rest of this suite with
# it silently.
UTIL="$SCRIPT_DIR/../utilities.sh"
skip_line=$(grep -n 'user onboarding already complete' "$UTIL" | head -1 | cut -d: -f1)
rec_line=$(grep -n 'ha_record_account "admin" "true"' "$UTIL" | head -1 | cut -d: -f1)
if [ -n "$skip_line" ] && [ -n "$rec_line" ] && [ "$skip_line" -lt "$rec_line" ]; then
    ok "an already-onboarded Home Assistant is left unclaimed"
else
    bad "an already-onboarded Home Assistant is left unclaimed (skip@${skip_line:-?} record@${rec_line:-?})"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
