#!/usr/bin/env bash
#
# LAN HTTPS is names on 443. These pins catch the old ports leaking back
# into the edge, the compose publish list, pairing, or the mDNS publisher.
#
#   bash scripts/tests/test_lan_https.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

CADDY="$REPO_ROOT/config/Caddyfile"
COMPOSE="$REPO_ROOT/docker-compose.yml"
MANAGER="$REPO_ROOT/config/homebrain-manager.service"
MDNS_UNIT="$REPO_ROOT/config/homebrain-mdns.service"
PUBLISH="$REPO_ROOT/scripts/publish_mdns.sh"

echo "== Caddy is Host routing on 443, not :8443/:8444 =="

if grep -qE ':8443|:8444' "$CADDY"; then
    bad "Caddyfile has no :8443/:8444"
else
    ok "Caddyfile has no :8443/:8444"
fi
for name in homebrain.local nc.homebrain.local vault.homebrain.local ha.homebrain.local; do
    if grep -q "$name" "$CADDY"; then
        ok "Caddyfile names $name"
    else
        bad "Caddyfile names $name"
    fi
done
if grep -q 'host.docker.internal:8000' "$CADDY"; then
    ok "dashboard proxies to host gunicorn :8000"
else
    bad "dashboard proxies to host gunicorn :8000"
fi
if grep -q 'reverse_proxy nextcloud:80' "$CADDY" \
   && grep -q 'reverse_proxy vaultwarden:80' "$CADDY" \
   && grep -q 'reverse_proxy homeassistant:8123' "$CADDY"; then
    ok "Caddy backends are container names on internal ports"
else
    bad "Caddy backends are container names on internal ports"
fi
if grep -q 'redir https://{host}{uri}' "$CADDY"; then
    ok "HTTP on 80 redirects to HTTPS"
else
    bad "HTTP on 80 redirects to HTTPS"
fi

echo "== compose does not publish the apps on the LAN =="

nc_ports="$(awk '/^  nextcloud:/{f=1; next} /^  [a-z]/{f=0} f' "$COMPOSE")"
ha_ports="$(awk '/^  homeassistant:/{f=1; next} /^  [a-z]/{f=0} f' "$COMPOSE")"
caddy_ports="$(awk '/^  caddy:/{f=1; next} /^  [a-z]/{f=0} f' "$COMPOSE")"

if grep -q '127.0.0.1:${NEXTCLOUD_PORT:-8080}:80' <<< "$nc_ports"; then
    ok "Nextcloud is loopback-only"
else
    bad "Nextcloud is loopback-only"
fi
if grep -q '"${NEXTCLOUD_PORT:-8080}:80"' <<< "$nc_ports"; then
    bad "Nextcloud is not published on all interfaces"
else
    ok "Nextcloud is not published on all interfaces"
fi
if grep -q '127.0.0.1:${HA_PORT:-8123}:8123' <<< "$ha_ports"; then
    ok "Home Assistant is loopback-only"
else
    bad "Home Assistant is loopback-only"
fi
if grep -qE '8443|8444' <<< "$caddy_ports"; then
    bad "Caddy does not publish :8443/:8444"
else
    ok "Caddy does not publish :8443/:8444"
fi
if grep -q '"80:80"' <<< "$caddy_ports" && grep -q '"443:443"' <<< "$caddy_ports"; then
    ok "Caddy publishes 80 and 443"
else
    bad "Caddy publishes 80 and 443"
fi
if grep -q 'host.docker.internal:host-gateway' <<< "$caddy_ports"; then
    ok "Caddy can reach the host gateway"
else
    bad "Caddy can reach the host gateway"
fi

echo "== gunicorn is not the LAN face =="

if grep -qF -- '--bind 0.0.0.0:8000' "$MANAGER"; then
    ok "manager binds :8000"
else
    bad "manager binds :8000"
fi
if grep -qF -- '--bind 0.0.0.0:80 ' "$MANAGER"; then
    bad "manager no longer binds :80"
else
    ok "manager no longer binds :80"
fi

echo "== mDNS publisher exists for the three aliases =="

if [[ -f "$MDNS_UNIT" && -f "$PUBLISH" ]]; then
    ok "mdns unit and publisher script are in tree"
else
    bad "mdns unit and publisher script are in tree"
fi
for name in nc.homebrain.local vault.homebrain.local ha.homebrain.local; do
    if grep -q "$name" "$PUBLISH"; then
        ok "publisher advertises $name"
    else
        bad "publisher advertises $name"
    fi
done
if grep -q 'refresh_vault_lan_ip' "$COMMON" && grep -q 'homebrain-mdns.service' "$COMMON"; then
    ok "LAN IP refresh restarts the mdns unit"
else
    bad "LAN IP refresh restarts the mdns unit"
fi

echo "== Nextcloud local mode is HTTPS on the nc name =="

nc_fn="$(awk '/^configure_nc_ha_proxy_settings\(\)/{f=1} f{print} f&&/^}/{exit}' "$COMMON")"
if grep -qF 'overwriteprotocol --value=http ' <<< "$nc_fn" \
   || grep -qF 'overwriteprotocol --value=http"' <<< "$nc_fn"; then
    bad "local mode does not force overwriteprotocol=http"
else
    ok "local mode does not force overwriteprotocol=http"
fi
if grep -q 'overwrite.cli.url --value="https://nc.homebrain.local"' <<< "$nc_fn"; then
    ok "local overwrite.cli.url is https://nc.homebrain.local"
else
    bad "local overwrite.cli.url is https://nc.homebrain.local"
fi
if grep -q 'homebrain.local:8080' <<< "$nc_fn"; then
    bad "proxy settings do not mention :8080"
else
    ok "proxy settings do not mention :8080"
fi

echo "== owner-facing Python has no :8443/:8444/:8080 URLs =="

if grep -nE 'NC_LOCAL_HTTPS_PORT|VAULT_LOCAL_HTTPS_PORT|homebrain.local:844|homebrain.local:8080' \
        "$REPO_ROOT/src/app.py"; then
    bad "app.py does not print the old ports"
else
    ok "app.py does not print the old ports"
fi

echo "== CA download is the box cert, always =="

if grep -q 'homebrain-vault-ca.pem' "$REPO_ROOT/src/app.py"; then
    bad "CA filename is not Vault-branded"
else
    ok "CA filename is not Vault-branded"
fi
if grep -q 'filename="homebrain-ca.pem"' "$REPO_ROOT/src/app.py"; then
    ok "CA filename is homebrain-ca.pem"
else
    bad "CA filename is homebrain-ca.pem"
fi
if grep -A2 'def vault_local_ca' "$REPO_ROOT/src/app.py" | grep -q 'is_local_mode'; then
    bad "CA endpoint is not gated on local mode"
else
    # The 404-in-remote-mode guard must be gone from the handler.
    if awk '/^def vault_local_ca/,/^def /' "$REPO_ROOT/src/app.py" | grep -q 'is_local_mode'; then
        bad "CA endpoint is not gated on local mode"
    else
        ok "CA endpoint is not gated on local mode"
    fi
fi
if grep -q 'This box'\''s certificate' "$REPO_ROOT/src/templates/dashboard.html"; then
    ok "dashboard labels the download as this box's certificate"
else
    bad "dashboard labels the download as this box's certificate"
fi
if grep -q 'deployment_mode == '\''local'\''' "$REPO_ROOT/src/templates/dashboard.html" \
        && grep -B5 'local-ca' "$REPO_ROOT/src/templates/dashboard.html" | grep -q 'deployment_mode'; then
    bad "CA download is not hidden in remote mode"
else
    ok "CA download is not hidden in remote mode"
fi

echo "== hosts aliases and VAULT_DOMAIN heal =="

HAVE_GNU_SED=false
sed --version 2>/dev/null | grep -q GNU && HAVE_GNU_SED=true
if [[ "$HAVE_GNU_SED" != "true" ]]; then
    printf '  SKIP  ensure_lan_hosts / VAULT_DOMAIN heal — needs GNU sed\n'
else
    harden_env_file() { :; }
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' RETURN
    LAN_HOSTS_FILE="$TMP/hosts"
    printf '127.0.0.1 localhost\n' > "$LAN_HOSTS_FILE"
    ensure_lan_hosts
    if grep -q 'nc.homebrain.local' "$LAN_HOSTS_FILE" \
       && grep -q 'vault.homebrain.local' "$LAN_HOSTS_FILE" \
       && grep -q 'ha.homebrain.local' "$LAN_HOSTS_FILE"; then
        ok "ensure_lan_hosts writes the three aliases"
    else
        bad "ensure_lan_hosts writes the three aliases"
    fi
    ensure_lan_hosts
    count="$(grep -c 'nc.homebrain.local' "$LAN_HOSTS_FILE" || true)"
    if [[ "$count" -eq 1 ]]; then
        ok "a second ensure_lan_hosts call does not duplicate"
    else
        bad "a second ensure_lan_hosts call does not duplicate (count=$count)"
    fi

    ENV_FILE="$TMP/vault.env"
    printf "VAULT_DOMAIN='https://homebrain.local:8443'\nVAULT_LAN_IP='192.168.1.1'\n" > "$ENV_FILE"
    VAULT_LAN_IP="192.168.1.1"
    VAULT_DOMAIN="https://homebrain.local:8443"
    unset NEWT_ID NEWT_SECRET PANGOLIN_DOMAIN
    hostname() { printf '192.168.1.1 \n'; }
    refresh_vault_lan_ip >/dev/null 2>&1
    got="$(env_value VAULT_DOMAIN)"
    if [[ "$got" == "https://vault.homebrain.local" ]]; then
        ok "local VAULT_DOMAIN heals off :8443"
    else
        bad "local VAULT_DOMAIN heals off :8443 (got '$got')"
    fi
    unset -f hostname
fi

echo "== lifecycle scripts install the mdns unit =="

for script in provision.sh update.sh; do
    if grep -q 'homebrain-mdns.service' "$SCRIPT_DIR/../$script"; then
        ok "$script installs homebrain-mdns.service"
    else
        bad "$script installs homebrain-mdns.service"
    fi
done
if grep -q 'stop caddy' "$SCRIPT_DIR/../redeploy_tunnels.sh"; then
    bad "redeploy_tunnels.sh no longer stops Caddy in remote mode"
else
    ok "redeploy_tunnels.sh no longer stops Caddy in remote mode"
fi

echo
echo "passed: $pass  failed: $fail"
[[ "$fail" -eq 0 ]]
