#!/usr/bin/env bash
#
# Regression tests for the tunnel-down-after-reboot incident.
#
# The bug this pins: Docker snapshots the host's /etc/resolv.conf when it
# creates a container's network sandbox and never refreshes it. On a Raspberry
# Pi, network-online.target fires before DHCP has a lease, so the snapshot can
# carry a nameserver from a previous LAN — and newt then cannot resolve
# PANGOLIN_ENDPOINT until somebody restarts the docker daemon. Measured: a box
# on 192.168.178.0/24 retrying 192.168.1.1 for four hours, tunnel down, every
# local status surface reporting "running".
#
# The fix is per-container DNS upstreams on newt. The first attempt was a
# systemd drop-in that delayed dockerd until a default route existed; it is
# removed here, and the removal is itself asserted so a box that installed it
# gets cleaned up.
#
# Second bug pinned: VAULT_LAN_IP was written once at provisioning time and
# never re-derived, so a box that moved networks (or a bare-metal restore onto
# a different LAN) kept naming an address it no longer held in Caddy's TLS SAN.
#
#   bash scripts/tests/test_tunnel_dns.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE="$REPO_ROOT/docker-compose.yml"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
skip=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
skipped() { printf '  SKIP  %s — %s\n' "$1" "$2"; skip=$((skip + 1)); }

expect() {  # expect <label> <expected> <actual>
    if [[ "$3" == "$2" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

# update_env_var's replace path uses GNU `sed -i`, which BSD sed (macOS)
# rejects. Linux box, Linux CI; a Mac developer gets SKIP, never a false pass.
HAVE_GNU_SED=false
sed --version 2>/dev/null | grep -q GNU && HAVE_GNU_SED=true

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== newt carries its own DNS upstreams =="

# Read the newt service block only: a `dns:` key anywhere else in the file
# would not fix the tunnel, and asserting on the whole file would pass anyway.
newt_block="$(awk '/^  newt:/{f=1; next} /^  [a-z]/{f=0} f' "$COMPOSE")"

if [[ -z "$newt_block" ]]; then
    bad "docker-compose.yml has a newt service"
else
    ok "docker-compose.yml has a newt service"
    if grep -qE '^\s+dns:' <<< "$newt_block"; then
        ok "newt declares dns:"
    else
        bad "newt declares dns: (the tunnel cannot resolve its endpoint without it)"
    fi
    # Public resolvers specifically. An RFC1918 address here would reintroduce
    # the bug: it is unreachable from a box that moved subnets, and Docker
    # waits out a timeout on it before trying anything else.
    for addr in 1.1.1.1 9.9.9.9; do
        if grep -qE "^\s+- +$addr\$" <<< "$newt_block"; then
            ok "newt forwards to $addr"
        else
            bad "newt forwards to $addr"
        fi
    done
    if grep -qE '^\s+- +(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' <<< "$newt_block"; then
        bad "no RFC1918 nameserver in newt's dns: (unreachable after a subnet change)"
    else
        ok "no RFC1918 nameserver in newt's dns:"
    fi
fi

echo "== the wait-for-default-route drop-in is gone =="

if [[ -f "$REPO_ROOT/config/docker-homebrain.conf" ]]; then
    bad "config/docker-homebrain.conf is deleted"
else
    ok "config/docker-homebrain.conf is deleted"
fi
# Call sites only — this file names the old function in its own assertions.
if grep -rq --exclude-dir=tests 'install_docker_wait_online' \
    "$REPO_ROOT/scripts" "$REPO_ROOT/config" 2>/dev/null; then
    bad "no caller still references install_docker_wait_online"
else
    ok "no caller still references install_docker_wait_online"
fi

echo "== remove_docker_wait_dropin cleans up a box that installed it =="

DOCKER_DROPIN_DIR="$TMP/dropin"
mkdir -p "$DOCKER_DROPIN_DIR"
DROPIN="$DOCKER_DROPIN_DIR/homebrain-wait-default-route.conf"
echo "[Service]" > "$DROPIN"
remove_docker_wait_dropin >/dev/null 2>&1
if [[ -f "$DROPIN" ]]; then
    bad "an installed drop-in is removed"
else
    ok "an installed drop-in is removed"
fi

if remove_docker_wait_dropin >/dev/null 2>&1; then
    ok "a second call is a no-op and still succeeds"
else
    bad "a second call is a no-op and still succeeds"
fi

# The offline branch returns early. It must still reach the cleanup, otherwise
# a box that provisions without a WAN keeps the boot delay forever.
if awk '/^install_deps_enable_docker\(\)/{f=1} f&&/^}/{exit} f' "$COMMON" \
    | grep -q 'remove_docker_wait_dropin'; then
    ok "install_deps_enable_docker reaches the cleanup"
else
    bad "install_deps_enable_docker reaches the cleanup"
fi

echo "== refresh_vault_lan_ip re-derives the Caddy SAN =="

if [[ "$HAVE_GNU_SED" != "true" ]]; then
    skipped "refresh_vault_lan_ip cases" "needs GNU sed (update_env_var uses sed -i)"
else
    # update_env_var chowns through harden_env_file; not available unprivileged.
    harden_env_file() { :; }

    # Stub `hostname` the way sibling tests stub `docker`: a function shadows
    # the binary for everything this shell calls.
    STUB_IP="192.168.178.112"
    hostname() { printf '%s \n' "$STUB_IP"; }

    ENV_FILE="$TMP/moved.env"
    printf "VAULT_LAN_IP='192.168.1.105'\n" > "$ENV_FILE"
    VAULT_LAN_IP="192.168.1.105"
    refresh_vault_lan_ip >/dev/null 2>&1
    expect "a box that moved subnet gets the new address" \
        "$STUB_IP" "$(env_value VAULT_LAN_IP)"

    # Unchanged: no rewrite. Proven by making the file read-only — a write
    # attempt would fail, and the call must still return 0.
    ENV_FILE="$TMP/same.env"
    printf "VAULT_LAN_IP='%s'\n" "$STUB_IP" > "$ENV_FILE"
    VAULT_LAN_IP="$STUB_IP"
    if refresh_vault_lan_ip >/dev/null 2>&1; then
        ok "an unchanged address is a no-op that succeeds"
    else
        bad "an unchanged address is a no-op that succeeds"
    fi
    expect "the unchanged value is left alone" "$STUB_IP" "$(env_value VAULT_LAN_IP)"

    # No network yet. Must not abort the restore/update that called us.
    ENV_FILE="$TMP/nolan.env"
    printf "VAULT_LAN_IP='192.168.1.105'\n" > "$ENV_FILE"
    VAULT_LAN_IP="192.168.1.105"
    STUB_IP=""
    hostname() { printf '\n'; }
    if refresh_vault_lan_ip >/dev/null 2>&1; then
        ok "a box with no address yet returns 0 (never aborts a restore)"
    else
        bad "a box with no address yet returns 0 (never aborts a restore)"
    fi
    expect "and leaves the old value rather than blanking it" \
        "192.168.1.105" "$(env_value VAULT_LAN_IP)"

    unset -f hostname
fi

echo "== every lifecycle path re-derives it =="

# Structural: the helper is worthless if the scripts that run on a moved or
# restored box never call it. provision_vault.sh must not keep its own copy.
for script in restore.sh update.sh provision_vault.sh; do
    if grep -q 'refresh_vault_lan_ip' "$SCRIPT_DIR/../$script"; then
        ok "$script calls refresh_vault_lan_ip"
    else
        bad "$script calls refresh_vault_lan_ip"
    fi
done
if grep -q 'update_env_var "VAULT_LAN_IP"' "$SCRIPT_DIR/../provision_vault.sh"; then
    bad "provision_vault.sh no longer carries its own inline copy"
else
    ok "provision_vault.sh no longer carries its own inline copy"
fi

echo
echo "passed: $pass  failed: $fail  skipped: $skip"
[[ "$fail" -eq 0 ]]
