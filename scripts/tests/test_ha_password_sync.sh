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

get_ha_cid() { echo "ha-test-cid"; }

docker() {
    case "$1" in
        inspect) echo "$RUNNING" ;;
        exec)    return "$CLI_RC" ;;
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
done

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
