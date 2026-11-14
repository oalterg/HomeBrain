#!/usr/bin/env bash
#
# Shell half of Remote access over the owner's private network
# (src/mesh.py, docs/plans/REMOTE_ACCESS.md).
#
# What this pins:
#   * is_local_mode has three copies (here, app.py, healthcheck.py). The
#     private-network name must make a box "remote" in this one too, or
#     redeploy configures Nextcloud and Vault as LAN-only while the dashboard
#     says otherwise — the half-applied state PR #256 was written against.
#   * ensure_tailscale never installs an unpinned build, and does nothing it
#     cannot finish when it is not root.
#   * the version is pinned, and the update only refreshes a client the owner
#     already chose to install.
#
#   bash scripts/tests/test_mesh.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

mode() {  # mode <env assignments...> -> "local" | "remote"
    (
        unset MESH_DOMAIN NEWT_ID NEWT_SECRET PANGOLIN_DOMAIN DEPLOYMENT_MODE
        for kv in "$@"; do export "${kv?}"; done
        if is_local_mode; then echo local; else echo remote; fi
    )
}
expect() {  # expect <label> <expected> <actual>
    if [[ "$3" == "$2" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

echo "is_local_mode"
expect "nothing configured is local" local "$(mode)"
expect "a private-network name is remote" remote \
    "$(mode MESH_DOMAIN=homebrain.tail1234.ts.net DEPLOYMENT_MODE=remote)"
expect "a private-network name with no mode set is remote" remote \
    "$(mode MESH_DOMAIN=homebrain.tail1234.ts.net)"
expect "opting out still wins over a private-network name" local \
    "$(mode MESH_DOMAIN=homebrain.tail1234.ts.net DEPLOYMENT_MODE=local)"
expect "Pangolin credentials alone are still remote" remote \
    "$(mode NEWT_ID=a NEWT_SECRET=b PANGOLIN_DOMAIN=home.example.com)"
expect "partial Pangolin credentials are still local" local \
    "$(mode NEWT_ID=a NEWT_SECRET=b)"

echo "ensure_tailscale"
out="$(ensure_tailscale "" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then ok "refuses without a pinned version"; else bad "installed with no pin: $out"; fi

# Not root and not installed: must bail before downloading anything.
if [[ "${EUID:-$(id -u)}" -ne 0 ]] && ! command -v tailscale >/dev/null 2>&1; then
    calls="$(mktemp)"
    (
        curl() { echo curl >> "$calls"; return 1; }
        ensure_tailscale "1.104.7" >/dev/null 2>&1
    )
    rc=$?
    if [[ $rc -ne 0 && ! -s "$calls" ]]; then ok "does not download when it cannot install"
    else bad "rc=$rc, downloads attempted: $(wc -l < "$calls")"; fi
    rm -f "$calls"
else
    printf '  SKIP  %s\n' "non-root bail-out — running as root or tailscale already present"
fi

echo "pins and wiring"
ver="$(jq -r '.tailscale.version // empty' "$REPO_ROOT/config/versions.json" 2>/dev/null)"
if [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then ok "versions.json pins tailscale ($ver)"
else bad "versions.json has no tailscale pin (got '$ver')"; fi

if grep -q 'command -v tailscale' "$REPO_ROOT/scripts/update.sh" \
    && grep -q 'ensure_tailscale' "$REPO_ROOT/scripts/update.sh"; then
    ok "update refreshes the pin only where the client is already installed"
else
    bad "update.sh does not gate ensure_tailscale on an existing install"
fi

if grep -q '^MESH_DOMAIN=' "$REPO_ROOT/config/.env.template"; then ok ".env.template carries MESH_DOMAIN"
else bad ".env.template is missing MESH_DOMAIN"; fi

# The dashboard's private-network port is written in two languages. One owner
# is src/mesh.py; utilities.sh must name the same port for the chat origin.
py_port="$(sed -n 's/^DASHBOARD = (\([0-9]*\),.*/\1/p' "$REPO_ROOT/src/mesh.py")"
if [[ -n "$py_port" ]] && grep -q "MESH_DOMAIN}:${py_port}" "$REPO_ROOT/scripts/utilities.sh"; then
    ok "chat origin uses the dashboard port from mesh.py ($py_port)"
else
    bad "utilities.sh and mesh.py disagree on the dashboard port (mesh.py: '$py_port')"
fi

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
