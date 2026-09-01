#!/usr/bin/env bash
#
# Regression tests for handover_pending — the gate that keeps newt/cloudflared
# down until the owner claims credentials.
#
# The bug this pins: deploy.sh skipped tunnels when install_creds.json existed,
# but first deploy writes .install_creds_staging and only promotes the json at
# the end. The skip never ran on the path it was written for, so a remote-mode
# first install published the factory-password window to the public hostname.
# restore.sh had no skip at all, so a wizard restore did the same.
#
#   bash scripts/tests/test_handover_tunnels.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# common.sh defaults INSTALL_DIR to /opt/homebrain at source time.
export INSTALL_DIR="$TMP/install"
mkdir -p "$INSTALL_DIR"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null
INSTALL_DIR="$TMP/install"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

clear_creds() {
    rm -f "$INSTALL_DIR/.install_creds_staging" "$INSTALL_DIR/install_creds.json"
}

held() {
    if handover_pending; then ok "$1"; else bad "$1 (expected held)"; fi
}
free() {
    if handover_pending; then bad "$1 (expected free)"; else ok "$1"; fi
}

echo "== handover_pending =="
clear_creds
free "neither file: claimed box / never-wizard"

clear_creds
: > "$INSTALL_DIR/.install_creds_staging"
held "staging only: first deploy (the case the json-only check missed)"

clear_creds
: > "$INSTALL_DIR/install_creds.json"
held "json only: handover page / re-deploy before claim"

clear_creds
: > "$INSTALL_DIR/.install_creds_staging"
: > "$INSTALL_DIR/install_creds.json"
held "both files: still held"

echo "== call sites =="
# deploy.sh and restore.sh must consult the helper. update.sh and
# redeploy_tunnels.sh must not — they run on a live, claimed box.
if grep -qE '^[[:space:]]*if handover_pending; then' "$REPO_ROOT/scripts/deploy.sh"; then
    ok "deploy.sh holds tunnels when handover_pending"
else
    bad "deploy.sh holds tunnels when handover_pending"
fi
if grep -qE '^[[:space:]]*if handover_pending; then' "$REPO_ROOT/scripts/restore.sh"; then
    ok "restore.sh holds tunnels when handover_pending"
else
    bad "restore.sh holds tunnels when handover_pending"
fi
if grep -q 'handover_pending' "$REPO_ROOT/scripts/update.sh"; then
    bad "update.sh does not consult handover_pending"
else
    ok "update.sh does not consult handover_pending"
fi
if grep -q 'handover_pending' "$REPO_ROOT/scripts/redeploy_tunnels.sh"; then
    bad "redeploy_tunnels.sh does not consult handover_pending"
else
    ok "redeploy_tunnels.sh does not consult handover_pending"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
