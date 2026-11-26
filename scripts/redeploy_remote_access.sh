#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"
hold_power_lock
SETUP_LOG_FILE="$LOG_DIR/main_setup.log"

# Ensure we capture all output
exec >> "$SETUP_LOG_FILE" 2>&1

log_info "=== Starting Remote Access Redeploy: $(date) ==="
load_env

# Retire the previous publication when remote access is turned off, including
# a public address switched on by hand. Clearing .env alone does not stop
# tailscaled.
if [[ -z "${MESH_DOMAIN:-}" ]] && command -v tailscale >/dev/null 2>&1; then
    # Best effort: a client that is installed but signed out, or stopped,
    # has nothing published and must not abort the redeploy.
    tailscale serve reset || true
fi

# `up` recreates Vaultwarden when its DOMAIN changed; `restart` below would not.
profiles=$(get_runtime_profiles)
docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} up -d --remove-orphans

# Reapply Proxy/Trust Configurations
# (Crucial if the trusted domain changed)
log_info "Refreshing Proxy and Trusted Domain Settings..."
# Explicitly non-fatal: it can now fail when the Nextcloud container is
# missing, and aborting here would skip the restarts and the verification
# below. It logs its own [ERROR].
configure_nc_ha_proxy_settings || true
bash "$SCRIPT_DIR/utilities.sh" refresh_openclaw_origins \
    || log_warn "Could not refresh chat origins; check the OpenClaw gateway."

# Selective Restart
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

# Verification
# Wait for HA to actually come back up to confirm success
wait_for_healthy "nextcloud" 120 || log_error "Nextcloud failed to restart cleanly."
wait_for_healthy "homeassistant" 120 || log_error "Home Assistant failed to restart cleanly."
if docker compose $(get_compose_args) ps -q vaultwarden 2>/dev/null | grep -q .; then
    wait_for_healthy "vaultwarden" 60 || log_warn "Vaultwarden failed to restart cleanly (non-fatal)."
fi

log_info "=== Remote Access Redeploy Complete ==="
