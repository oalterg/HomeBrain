#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"
SETUP_LOG_FILE="$LOG_DIR/main_setup.log"

# Ensure we capture all output
exec >> "$SETUP_LOG_FILE" 2>&1

log_info "=== Starting Tunnel Redeploy: $(date) ==="
load_env

# 1. Clean Slate: Stop ALL potential tunnel services first.
# This prevents zombie containers when switching profiles (e.g. CF -> Pangolin).
# Docker compose 'up' with profiles ignores services not in the active profile,
# so we must manually ensure the old ones are dead.
log_info "Stopping any existing tunnel services..."
docker compose --env-file "$ENV_FILE" $(get_compose_args) stop newt cloudflared-nc cloudflared-ha 2>/dev/null || true

# 2. Identify and Pull New Profile
profiles=$(get_tunnel_profiles)
log_info "Active Tunnel Profile: ${profiles:-None}"

vault_profiles=$(get_vault_profiles)
if [[ -n "$profiles" || -n "$vault_profiles" ]]; then
    docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} ${vault_profiles} pull
    docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} ${vault_profiles} up -d --remove-orphans
fi
# In remote mode, ensure Caddy is stopped (mode flip from local→remote
# leaves it running otherwise — its profile is now opt-out).
if [[ -z "$vault_profiles" ]]; then
    docker compose --env-file "$ENV_FILE" $(get_compose_args) stop caddy 2>/dev/null || true
fi
if [[ -z "$profiles" && -z "$vault_profiles" ]]; then
    log_info "No tunnel and no vault profile configured."
fi

# 3. Reapply Proxy/Trust Configurations
# (Crucial if the trusted domain changed)
log_info "Refreshing Proxy and Trusted Domain Settings..."
configure_nc_ha_proxy_settings

# 4. Selective Restart
# Home Assistant reads configuration.yaml on startup, so it MUST be restarted if proxies changed.
# Nextcloud reads config.php on every request, so strict restart isn't always needed, 
# but we restart to be safe and ensure clean state.
log_info "Restarting core services to apply changes..."
restart_services=(homeassistant nextcloud)
# Vaultwarden's DOMAIN env is recomputed per deployment mode; restart so it
# picks up the new value (also forces WS reconnects on existing clients).
if docker compose $(get_compose_args) ps -q vaultwarden 2>/dev/null | grep -q .; then
    restart_services+=(vaultwarden)
fi
if docker compose $(get_compose_args) ps -q caddy 2>/dev/null | grep -q .; then
    restart_services+=(caddy)
fi
docker compose --env-file "$ENV_FILE" $(get_compose_args) restart "${restart_services[@]}"

# 5. Verification
# Wait for HA to actually come back up to confirm success
wait_for_healthy "nextcloud" 120 || log_error "Nextcloud failed to restart cleanly."
wait_for_healthy "homeassistant" 120 || log_error "Home Assistant failed to restart cleanly."
if docker compose $(get_compose_args) ps -q vaultwarden 2>/dev/null | grep -q .; then
    wait_for_healthy "vaultwarden" 60 || log_warn "Vaultwarden failed to restart cleanly (non-fatal)."
fi

log_info "=== Tunnel Redeploy Complete ==="