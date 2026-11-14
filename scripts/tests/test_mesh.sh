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

# A factory reset that leaves the box signed in keeps it published after the
# password and data are gone — the newt defect PR #145 fixed, one layer down.
nr="$REPO_ROOT/scripts/nuclear_reset.sh"
if grep -q 'tailscale serve reset' "$nr" && grep -q 'tailscale logout' "$nr"; then
    ok "factory reset unpublishes and signs out"
else
    bad "nuclear_reset.sh leaves the private network signed in"
fi

# The dashboard's private-network port is written in two languages. One owner
# is src/mesh.py; utilities.sh must name the same port for the chat origin.
py_port="$(sed -n 's/^DASHBOARD = (\([0-9]*\),.*/\1/p' "$REPO_ROOT/src/mesh.py")"
if [[ -n "$py_port" ]] && grep -q "MESH_DOMAIN}:${py_port}" "$REPO_ROOT/scripts/utilities.sh"; then
    ok "chat origin uses the dashboard port from mesh.py ($py_port)"
else
    bad "utilities.sh and mesh.py disagree on the dashboard port (mesh.py: '$py_port')"
fi

echo "LAN HTTPS beside remote access"
# tailscaled binds <tailnet address>:443. Docker's "443 on every address" then
# fails, Caddy does not start and homebrain.local goes dark (seen on .69).
edge() {  # edge <MESH_DOMAIN> <lan_ip> -> "https|loopback"
    (
        update_env_var() { :; }
        MESH_DOMAIN="$1"; unset LAN_EDGE_HTTPS LAN_EDGE_LOOPBACK
        refresh_lan_edge_bind "$2" >/dev/null 2>&1
        echo "${LAN_EDGE_HTTPS}|${LAN_EDGE_LOOPBACK}"
    )
}
expect "remote access on: 443 on the LAN address and loopback only" \
    "192.168.1.10:443|127.0.0.1:443" "$(edge hb.t.ts.net 192.168.1.10)"
expect "remote access off: every address" "|" "$(edge "" 192.168.1.10)"
expect "no LAN address known: every address" "|" "$(edge hb.t.ts.net "")"
if grep -q '"${LAN_EDGE_HTTPS:-443}:443"' "$REPO_ROOT/docker-compose.yml" \
    && grep -q '"${LAN_EDGE_LOOPBACK:-127.0.0.1:44380}:443"' "$REPO_ROOT/docker-compose.yml" \
    && grep -B3 'up -d --remove-orphans' "$REPO_ROOT/scripts/redeploy_tunnels.sh" | grep -q refresh_vault_lan_ip \
    && grep -q 'utilities.sh refresh_lan_edge' "$REPO_ROOT/config/homebrain-manager.service"; then
    ok "compose, redeploy and boot all use the narrowed bind"
else
    bad "the narrowed 443 bind is not wired into compose, redeploy and boot"
fi

echo "identity carried in a backup"
# The file is the device. It goes only into an encrypted archive, restore only
# stages it, and resume is the one place it is put where the client reads it.
work="$(mktemp -d)"
state="$work/tailscaled.state"; staged="$work/var/mesh_identity.state"
printf 'OLD-BOX' > "$state"
cap() {  # cap <MESH_DOMAIN> <encrypted> -> staging dir listing
    ( rm -rf "$work/stage"; mkdir -p "$work/stage"
      MESH_DOMAIN="$1" MESH_STATE_FILE="$state"
      capture_mesh_identity "$work/stage" "$2" >/dev/null 2>&1
      ls "$work/stage" )
}
expect "captured into an encrypted backup" "mesh_identity.state" "$(cap hb.t.ts.net true)"
expect "left out of an unencrypted backup" "" "$(cap hb.t.ts.net false)"
expect "left out when remote access is off" "" "$(cap "" true)"

mkdir -p "$work/extracted"; printf 'OLD-BOX' > "$work/extracted/mesh_identity.state"
(
    tailscale() { echo '{"BackendState":"NeedsLogin"}'; }
    MESH_IDENTITY_FILE="$staged" MESH_STATE_FILE="$state.live"
    stage_mesh_identity "$work/extracted" >/dev/null 2>&1
)
if [[ "$(cat "$staged" 2>/dev/null)" == "OLD-BOX" && ! -e "$state.live" ]]; then
    ok "restore stages the identity and does not apply it"
else
    bad "restore staged nothing, or applied the identity itself"
fi
rm -f "$staged"
(
    tailscale() { echo '{"BackendState":"Running"}'; }
    MESH_IDENTITY_FILE="$staged"
    stage_mesh_identity "$work/extracted" >/dev/null 2>&1
)
if [[ ! -e "$staged" ]]; then ok "a box that is still signed in stages nothing"
else bad "staged a second identity onto a signed-in box"; fi

printf 'OLD-BOX' > "$staged"; printf 'NEW-BOX' > "$state.live"
order="$work/order"
(
    systemctl() { echo "$1" >> "$order"; }
    tailscaled() { :; }
    MESH_IDENTITY_FILE="$staged" MESH_STATE_FILE="$state.live"
    resume_mesh_identity >/dev/null 2>&1
); rc=$?
if [[ $rc -eq 0 && "$(cat "$state.live")" == "OLD-BOX" && ! -e "$staged" \
      && "$(cat "$state.live.before-resume")" == "NEW-BOX" \
      && "$(tr '\n' ' ' < "$order")" == "stop start " ]]; then
    ok "resume stops the client, swaps the identity in, starts it, and keeps one copy"
else
    bad "resume: rc=$rc order='$(tr '\n' ' ' < "$order" 2>/dev/null)' live='$(cat "$state.live" 2>/dev/null)'"
fi
( MESH_IDENTITY_FILE="$work/none" resume_mesh_identity >/dev/null 2>&1 ); rc=$?
if [[ $rc -ne 0 ]]; then ok "resume with nothing staged fails"; else bad "resume with nothing staged returned 0"; fi
rm -rf "$work"

if grep -q 'capture_mesh_identity "\$STAGING_DIR" "\$ENCRYPT"' "$REPO_ROOT/scripts/backup.sh" \
    && grep -q 'stage_mesh_identity "\$TMP_DIR"' "$REPO_ROOT/scripts/restore.sh" \
    && ! grep -q 'resume_mesh_identity' "$REPO_ROOT/scripts/restore.sh"; then
    ok "backup captures, restore stages, and restore never resumes by itself"
else
    bad "backup.sh / restore.sh are not wired to the identity helpers as designed"
fi
if grep -q 'mesh_identity.state' "$nr"; then ok "factory reset removes a staged identity"
else bad "nuclear_reset.sh leaves a staged device key behind"; fi

echo "no HomeBrain-run account service"
if [[ ! -e "$REPO_ROOT/registrar" ]] \
    && ! grep -rqiE 'REGISTRAR_URL|/api/cloud/register|HEARTBEAT_URL' \
        "$REPO_ROOT/src" "$REPO_ROOT/scripts/healthcheck.py" "$REPO_ROOT/config/.env.template"; then
    ok "nothing in the box's code calls an account or heartbeat service"
else
    bad "a registrar or heartbeat reference is still in the tree"
fi
# Factory scripts that still pass the retired arguments must not fail on them.
if grep -q -- '--registrar-url|--registrar-secret) shift' "$REPO_ROOT/scripts/provision.sh" \
    && ! grep -q '^REGISTRAR_' "$REPO_ROOT/scripts/provision.sh"; then
    ok "provision.sh accepts and ignores the retired registrar arguments"
else
    bad "provision.sh still writes, or now rejects, the registrar arguments"
fi

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
