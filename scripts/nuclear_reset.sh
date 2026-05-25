#!/bin/bash
set -euo pipefail

# =============================================================================
# HomeBrain Nuclear Reset (Factory Wipe)
# =============================================================================
# This script performs a complete, irreversible factory reset of the device.
# It is ONLY ever invoked by the HomeBrain manager after strict confirmation.
#
# What it does:
#   - Stops all services cleanly
#   - Destroys ALL Docker named volumes
#   - Wipes all user data (Nextcloud, HA, Vault, OpenClaw workspace, tokens, etc.)
#   - Optionally wipes AI models (default) and/or AI runtime binaries
#   - Deletes .env, .secret_key, and all setup markers
#   - Generates a brand new master password (via /dev/urandom)
#   - Writes fresh install_creds.json for the standard handover flow
#   - Reboots the device
#
# What it NEVER touches:
#   - /mnt/backup and its fstab entry
#   - Factory config (FACTORY_PASSWORD + baked tunnel secrets)
#   - The /opt/homebrain application code itself
#   - GPU hardening (udev, modprobe, kernel params)
#
# Safety:
#   - Must be run as root
#   - Uses a lock file
#   - Is designed to be re-runnable after power loss (best-effort idempotency)
#   - Writes progress to the standard task status file
#
# =============================================================================

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# Load .env to get correct data paths (NEXTCLOUD_DATA_DIR, VAULT_DATA_DIR, etc.)
# On re-run after power loss .env may already be deleted — that's fine, defaults apply.
if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
fi

LOCK_FILE="/var/run/homebrain-nuclear-reset.lock"
STATUS_FILE="/tmp/homebrain_task_status.json"
LOG_FILE="$LOG_DIR/nuclear_reset.log"

# --- Helpers -----------------------------------------------------------

log() {
    echo "[NUCLEAR] $1" | tee -a "$LOG_FILE" >&2
}

write_status() {
    local status="$1"
    local message="$2"
    cat > "$STATUS_FILE" <<EOF
{"status": "$status", "message": "$message", "log_type": "setup"}
EOF
    chmod 644 "$STATUS_FILE" 2>/dev/null || true
}

die() {
    log "FATAL: $1"
    write_status "error" "Nuclear reset failed: $1"
    rm -f "$LOCK_FILE"
    exit 1
}

# --- Argument Parsing (from manager) -----------------------------------

WIPE_AI_MODELS="${1:-true}"
WIPE_AI_RUNTIME="${2:-false}"

# --- Pre-flight --------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    die "Must be run as root"
fi

exec 200>"$LOCK_FILE"
flock -n 200 || die "Another nuclear reset is already running."

mkdir -p "$LOG_DIR"
echo "=== NUCLEAR RESET STARTED $(date -Iseconds) ===" > "$LOG_FILE"
log "Wipe AI models: $WIPE_AI_MODELS | Wipe AI runtime: $WIPE_AI_RUNTIME"

write_status "running" "Nuclear reset in progress — stopping services..."

# --- 1. Clean shutdown -------------------------------------------------

log "Stopping OpenClaw daemon (if running)..."
sudo -u homebrain openclaw daemon stop 2>/dev/null || true
pkill -x "openclaw" 2>/dev/null || true

log "Stopping llama-server and whisper-server..."
systemctl stop llama-server whisper-server whisper-proxy 2>/dev/null || true
systemctl disable llama-server whisper-server whisper-proxy 2>/dev/null || true

log "Stopping Docker stack..."
if [[ -f "$ENV_FILE" ]]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down --remove-orphans 2>/dev/null || true
else
    docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
fi

# --- 2. Destroy Docker named volumes -----------------------------------

write_status "running" "Nuclear reset in progress — destroying volumes..."

log "Removing all Docker named volumes..."
if [[ -f "$ENV_FILE" ]]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down -v --remove-orphans 2>/dev/null || true
else
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans 2>/dev/null || true
fi

# --- 3. Wipe host user data (the nuclear part) -------------------------

write_status "running" "Nuclear reset in progress — wiping user data..."

log "Wiping Nextcloud data..."
rm -rf -- "${NEXTCLOUD_DATA_DIR:-/home/homebrain/nextcloud-data}" 2>/dev/null || true

log "Wiping Vault data..."
rm -rf -- "${VAULT_DATA_DIR:-/home/homebrain/vault-data}" 2>/dev/null || true

log "Wiping OpenClaw workspace, tokens, and MCP state..."
rm -rf -- "${HOMEBRAIN_HOME:-/home/homebrain}/.openclaw" 2>/dev/null || true

if [[ "$WIPE_AI_MODELS" == "true" ]]; then
    log "Wiping AI models (as requested)..."
    rm -rf -- "${HOMEBRAIN_HOME:-/home/homebrain}/models" 2>/dev/null || true
else
    log "Preserving AI models (user opted out)."
fi

if [[ "$WIPE_AI_RUNTIME" == "true" ]]; then
    log "Wiping AI runtime binaries (as requested)..."
    rm -rf -- "${HOMEBRAIN_HOME:-/home/homebrain}/ai-runtime" 2>/dev/null || true
else
    log "Preserving AI runtime binaries (default)."
fi

# --- 4. Erase all runtime configuration & markers ----------------------

write_status "running" "Nuclear reset in progress — erasing configuration..."

log "Removing .env, .secret_key, and all setup state..."
rm -f "$ENV_FILE" 2>/dev/null || true
rm -f "$INSTALL_DIR/.secret_key" 2>/dev/null || true
rm -f "$INSTALL_DIR/.setup_complete" 2>/dev/null || true
rm -f "$INSTALL_DIR/.setup_started" 2>/dev/null || true
rm -f "$INSTALL_DIR/install_creds.json" 2>/dev/null || true
rm -f "$INSTALL_DIR/.install_creds_staging" 2>/dev/null || true
rm -f "$INSTALL_DIR/.first_boot_update_done" 2>/dev/null || true
rm -f "$INSTALL_DIR/docker-compose.override.yml" 2>/dev/null || true

log "Removing backup cron..."
rm -f /etc/cron.d/homebrain-backup 2>/dev/null || true

# --- 5. Generate brand new master password & handover credentials ------

write_status "running" "Nuclear reset in progress — generating new credentials..."

log "Generating new master password..."
NEW_PASS=$(head -c 100 /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9') || true
NEW_PASS="${NEW_PASS:0:16}"

if [[ ${#NEW_PASS} -lt 16 ]]; then
    die "Failed to generate secure password from /dev/urandom"
fi

log "Writing fresh install_creds.json for new setup flow..."

CREDS_TMP=$(mktemp)
cat > "$CREDS_TMP" <<EOF
{
  "username": "admin",
  "password": "$NEW_PASS",
  "domain": null,
  "generated_at": $(date +%s)
}
EOF

chmod 600 "$CREDS_TMP"
chown root:root "$CREDS_TMP"

mv "$CREDS_TMP" "$INSTALL_DIR/install_creds.json"

log "New master password generated and staged for handover."

# --- 6. Final cleanup & reboot -----------------------------------------

write_status "success" "Nuclear reset complete. Rebooting now..."

log "Syncing disks..."
sync

log "=== NUCLEAR RESET COMPLETE — REBOOTING IN 5 SECONDS ==="
sleep 5

rm -f "$LOCK_FILE"

reboot
