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
# Every script that can bring a tunnel up must consult the helper, including
# redeploy_tunnels.sh: provision.sh reaches it on any box with .setup_complete,
# and deploy.sh sets that marker inside the handover window.
for script in deploy.sh restore.sh redeploy_tunnels.sh; do
    if grep -qE '^[[:space:]]*if handover_pending; then' "$REPO_ROOT/scripts/$script"; then
        ok "$script holds tunnels when handover_pending"
    else
        bad "$script holds tunnels when handover_pending"
    fi
done

# update.sh is the deliberate exemption, not an oversight: a box deployed under
# the pre-guard code has a live tunnel AND unclaimed creds, and gating the
# update would drop its remote access mid-upgrade. Pinned so the exemption is a
# decision someone has to revisit on purpose — if you are removing this, read
# handover_pending's comment in common.sh first.
if grep -q 'handover_pending' "$REPO_ROOT/scripts/update.sh"; then
    bad "update.sh exemption is intact (it now consults handover_pending)"
else
    ok "update.sh exemption is intact"
fi

echo "== teardown =="
# Dropping the profiles only stops a tunnel being STARTED. Without an explicit
# stop, a tunnel that is already running survives the whole handover window and
# the guard above is decorative.
for script in deploy.sh restore.sh; do
    # Anchored: a prose mention of the helper in a comment is not a call.
    if grep -qE '^[[:space:]]*stop_tunnel_services([[:space:]]|$)' "$REPO_ROOT/scripts/$script"; then
        ok "$script stops tunnels that are already running"
    else
        bad "$script stops tunnels that are already running"
    fi
done

# Drift guard: a tunnel service added to docker-compose.yml but not to
# stop_tunnel_services stays published through handover. Collect every service
# carrying a pangolin/cloudflare profile and demand the helper names it.
stop_body=$(sed -n '/^stop_tunnel_services()/,/^}/p' "$COMMON")
publishers=$(awk '
    /^  [a-zA-Z0-9_-]+:/ { svc = $1; sub(/:$/, "", svc); inprof = 0 }
    /^    profiles:/      { inprof = 1; next }
    /^    [a-zA-Z]/       { inprof = 0 }
    inprof && /pangolin|cloudflare/ { print svc }
' "$REPO_ROOT/docker-compose.yml" | sort -u)
if [[ -z "$publishers" ]]; then
    bad "found no profile-gated tunnel services in docker-compose.yml (parser broken?)"
else
    for svc in $publishers; do
        if grep -q -- "$svc" <<<"$stop_body"; then
            ok "stop_tunnel_services covers $svc"
        else
            bad "stop_tunnel_services covers $svc"
        fi
    done
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
