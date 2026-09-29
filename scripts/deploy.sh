#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"
SETUP_LOG_FILE="$LOG_DIR/main_setup.log"

# Start every deploy with a fresh log, keeping exactly one predecessor.
#
# Not just hygiene: the setup wizard streams this file and decides it is
# finished the moment the text contains "Deployment Complete - Ready for
# Handover". Appending across runs meant a re-provision showed the PREVIOUS
# run's marker, so the wizard jumped straight to the credentials screen for an
# install that had not happened yet. It also broke log polling during the #145
# hardware E2E for the same reason.
# -s, not -f: the manager rolls this file itself before spawning us, and the
# `>> main_setup.log` in that command re-creates it empty before this line
# runs. Rolling an empty file would overwrite the predecessor we just kept,
# throwing away the previous install's log — the one thing anyone debugging a
# failed install actually wants.
if [[ -s "$SETUP_LOG_FILE" ]]; then
    mv -f "$SETUP_LOG_FILE" "${SETUP_LOG_FILE}.1" 2>/dev/null || true
fi

if [ -t 1 ]; then :; else exec >> "$SETUP_LOG_FILE" 2>&1; fi

log_info "=== Starting Deployment: $(date) ==="

# Resilience: Ensure time is correct for SSL/Tokens
wait_for_time_sync

load_env

# --- 0a. Verify HomeBrain OS user and group membership ---
ensure_homebrain_user

# --- 0a.1. Clear stale host bind-mount data on fresh installs ---
# `docker compose down -v` wipes named volumes but leaves host bind mounts.
# When re-provisioning, leftover /home/homebrain/{nextcloud,vault}-data makes
# the Nextcloud entrypoint loop with "Login is invalid because files already
# exist for this user" until deploy.sh's install-status wait times out.
#
# A fresh install is identified by the absence of $INSTALL_DIR/.setup_complete;
# under that condition any data in these bind-mount dirs is by definition
# stale (no admin password has ever been claimed). On a re-deploy of a working
# install (.setup_complete present) we MUST NOT wipe — that would destroy
# real user data.
if [[ ! -f "$INSTALL_DIR/.setup_complete" ]]; then
    clear_partial_install
fi

# --- 0b. Migrate legacy /home/admin data to /home/homebrain (no-op on fresh installs) ---
bash "$SCRIPT_DIR/utilities.sh" migrate || log_warn "Migration step failed (non-fatal on fresh installs)."

# --- 0c. Ensure dependencies installed ---
install_deps_enable_docker

# --- 1. Docker Stack Deployment ---
log_info "Deploying Docker stack, this can take while..."
# Ensure docker is running
if ! systemctl is-active --quiet docker; then
    echo "Waiting for Docker service..."
    systemctl start docker
    sleep 5
fi

# 1a. Pull Images
# Try to pull updates, but do not fail if offline (fallback to factory images)
docker compose --env-file "$ENV_FILE" $(get_compose_args) pull || log_warn "Image pull failed/skipped. Using pre-loaded local images."

# 1b. Start Database FIRST (Fix for 'Not Installed' race condition)
log_info "Starting Database..."
docker compose --env-file "$ENV_FILE" $(get_compose_args) up -d --remove-orphans db
wait_for_healthy "db" 120 || die "DB failed to start. Aborting deployment."

# 1b.1. Provision the Vault DB+user before bringing the vault container up.
# Idempotent — generates secrets on first run, no-ops afterwards.
log_info "Provisioning HomeBrain Vault state..."
bash "$SCRIPT_DIR/provision_vault.sh" || log_warn "Vault provisioning had issues (non-fatal); will retry from dashboard."
load_env  # Reload vault env vars set by provision_vault.sh

# 1c. Start Remaining Services
profiles=$(get_tunnel_profiles)

# Do not publish newt/cloudflared until the owner claims credentials.
# cleanup_credentials starts them via activate_tunnels after that click.
# See handover_pending in common.sh — gating on install_creds.json alone
# misses first deploy, when the file is still .install_creds_staging, and
# a wizard restore keeps them staged until finish_restore.
#
# Dropping the profiles is not enough: `up` leaves an already-running
# tunnel up. stop_tunnel_services takes it down. Restore.sh has the same
# hold — otherwise this skip just delayed publish until the stack restart,
# while Nextcloud still carried the backup's trusted domains.
if handover_pending; then
    log_info "Handover pending. Skipping tunnel startup until credentials are claimed."
    profiles=""
    # Not redundant with the empty profiles: `up` leaves services outside the
    # active profile running, so a tunnel that is already up would sail through
    # the whole handover window. See stop_tunnel_services in common.sh.
    stop_tunnel_services
fi

ensure_searxng_secret
vault_profiles=$(get_vault_profiles)
log_info "Starting Stack with Tunnel Profile: ${profiles:-None} · Vault Profile: ${vault_profiles:-None}"
docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} ${vault_profiles} up -d --remove-orphans

# 1d. Verification
wait_for_healthy "nextcloud" 400 || die "Nextcloud failed to start."
wait_for_healthy "homeassistant" 120 || die "Homeassistant failed to start."
if [[ "${VAULT_ENABLED:-true}" == "true" ]]; then
    wait_for_healthy "vaultwarden" 120 || log_warn "Vaultwarden failed health check (non-fatal — check /api/logs/vaultwarden)."
fi

# 1e. Create Home Assistant Admin Account
log_info "Hardening Home Assistant Admin account..."
bash "$SCRIPT_DIR/utilities.sh" ha_admin "$MASTER_PASSWORD" || log_error "HA Admin creation failed."

# --- 2. Post-Deploy Proxy Configuration ---
log_info "Applying Nextcloud and Homeassistant Proxy Settings..."
NC_CID=$(get_nc_cid)

# Wait for NC internal install to be verified
log_info "Waiting for Nextcloud installation status to confirm 'true'..."
TIMEOUT=120
while [[ $TIMEOUT -gt 0 ]]; do
    # Suppress stderr to avoid flooding log with 'not installed' errors while waiting
    if docker exec -u www-data "$NC_CID" php occ status 2>/dev/null | grep -q "installed: true"; then
        log_info "Nextcloud installation verified."
        break
    fi
    # If the DB is up but NC is stuck, the split startup above usually fixes it.
    # But if we are here, we log a heartbeat.
    if (( TIMEOUT % 10 == 0 )); then
        log_info "Still waiting for Nextcloud ($TIMEOUT seconds remaining)..."
    fi
    sleep 5
    ((TIMEOUT-=5))
done

[[ $TIMEOUT -le 0 ]] && die "Nextcloud installation timed out. Check if the database password in .env matches the volume data."

configure_nc_ha_proxy_settings || die "Proxy configuration failed."

log_info "Applying Nextcloud Redis Configuration..."
configure_nextcloud_redis || log_warn "Redis configuration failed (non-fatal)."

# Restart to apply proxy settings (Safe restart)
# We do not restart DB here, only the frontends
docker compose $(get_compose_args) restart nextcloud homeassistant

wait_for_healthy "nextcloud" 120 || die "Nextcloud failed to get healthy after proxy config" 
wait_for_healthy "homeassistant" 120 || die "Homeassistant failed to get healthy after proxy config" 

# --- 3. Cron Setup ---
log_info "Configuring Cron..."
# Use the utility script to ensure consistency and use systemctl
bash "$SCRIPT_DIR/utilities.sh" cron || log_error "Nextcloud cron configuration failed."

# --- 4. Hardening ---
# Disable wireless on headless appliance (Pi). Desktop (x86) keeps wifi/bluetooth.
if [[ -d "/boot/firmware" ]]; then
    log_info "Disabling Wireless interfaces..."
    if command -v rfkill >/dev/null 2>&1; then
        rfkill block wifi || log_warn "WiFi could not be disabled (possibly already disabled or unavailable)."
        rfkill block bluetooth || log_warn "Bluetooth could not be disabled (possibly already disabled or unavailable)."
    else
        log_warn "rfkill command not found. Wireless interfaces not disabled."
    fi
fi

log_info "=== Deployment Complete ==="

# ATOMIC HANDOVER: Move credentials from staging to final path
# This ensures the UI only shows the Success screen when we are actually done.
#
# .restoring means we are the first half of a restore: the owner's data is
# still to come, so "actually done" is not now. utilities.sh finish_restore
# promotes at the end of the chain instead — on failure too, or a failed
# restore would leave the master password unreadable forever.
if [ -f "$INSTALL_DIR/.restoring" ]; then
    log_info "Restore pending — credentials stay staged until it finishes."
else
    promote_install_creds
fi

# Mark setup as complete before signaling the UI
touch "$INSTALL_DIR/.setup_complete"

# Signal specifically for the UI to pick up
echo "Deployment Complete - Ready for Handover"

# After handover, so the dashboard is up while a model download and, on Arc, a
# llama.cpp source build run. start_ai_auto_setup returns as soon as the
# transient unit is queued; it logs the skip itself when this GPU is not
# first-class, the owner opted out, or the stack is already installed.
start_ai_auto_setup
