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
#   - Reboots the device back into the first-boot setup wizard, which mints the
#     new master password and recovery phrase (see app.py:start_setup)
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

# --- 2. Tear the whole project down, volumes and all -------------------

write_status "running" "Nuclear reset in progress — destroying volumes..."

# Profile-gated services (newt, cloudflared, proton-bridge) are invisible to a
# plain `down`: Compose only acts on profiles that are currently active. A wipe
# that leaves newt running keeps the device published to its tunnel after the
# admin password and all data are gone — precisely the exposure deploy.sh
# refuses to create on a fresh install. So ask Compose for every profile it
# knows about and take the entire project down in one pass.
compose_args=(-f "$COMPOSE_FILE")
[[ -f "$ENV_FILE" ]] && compose_args+=(--env-file "$ENV_FILE")
while read -r profile; do
    [[ -n "$profile" ]] && compose_args+=(--profile "$profile")
done < <(docker compose -f "$COMPOSE_FILE" config --profiles 2>/dev/null || true)

log "Stopping Docker stack (all profiles) and destroying volumes..."
docker compose "${compose_args[@]}" down -v --remove-orphans 2>/dev/null || true

# A container whose service was renamed or dropped from the compose file is not
# reachable through `down` at any profile setting. This project's service list
# has churned across releases, so sweep anything still carrying the project
# label — a factory wipe has to mean nothing survives.
project="${COMPOSE_PROJECT_NAME:-$(basename "$(dirname "$COMPOSE_FILE")")}"
leftover=$(docker ps -aq --filter "label=com.docker.compose.project=${project}" 2>/dev/null || true)
if [[ -n "$leftover" ]]; then
    log "Removing $(echo "$leftover" | wc -l) leftover container(s) from project '${project}'..."
    echo "$leftover" | xargs docker rm -f 2>/dev/null || true
fi

# --- 3. Wipe host user data (the nuclear part) -------------------------

write_status "running" "Nuclear reset in progress — wiping user data..."

log "Wiping Nextcloud data..."
NC_DATA="${NEXTCLOUD_DATA_DIR:-/home/homebrain/nextcloud-data}"
rm -rf -- "$NC_DATA" 2>/dev/null || true

# A files drive is not preserved the way the backup drive is: its contents were
# just destroyed, so keeping the mount keeps nothing. Leaving the fstab entry
# behind gives a "factory" box a drive mounted at a path the fresh .env does
# not reference — an orphan the owner has no way to interpret. Unmount it and
# forget it; Storage will offer the bare drive again.
if mountpoint -q "$NC_DATA"; then
    log "Releasing files drive at $NC_DATA"
    umount "$NC_DATA" 2>/dev/null || umount -l "$NC_DATA" 2>/dev/null || true
    sed -i "\|[[:space:]]${NC_DATA}[[:space:]]|d" /etc/fstab
fi

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
# Survived a reset and made the next fresh install describe itself as a restore
# in the wizard, naming an archive from the box that no longer exists.
rm -f "$INSTALL_DIR/.restoring" 2>/dev/null || true

log "Removing backup cron..."
rm -f /etc/cron.d/homebrain-backup 2>/dev/null || true

# --- 5. Final cleanup & reboot -----------------------------------------
#
# Deliberately no credentials are minted here. This script used to generate a
# password and write install_creds.json "for the standard handover flow", but
# nothing downstream ever consumed it: .env is deleted just above, so that
# password was applied to no service, and no recovery phrase existed alongside
# it. index() shows the handover page whenever install_creds.json is present,
# so the operator was handed a master password that unlocked nothing and — once
# the .txt download shipped — could save it to disk as a recovery sheet. Worse,
# cleanup_credentials() refuses to clear that page until cloud registration
# completes, wedging a freshly wiped box on a screen full of fiction.
#
# The setup wizard is the single place that mints a master password and its
# recovery phrase (app.py:start_setup) and applies them to every service. With
# .env and .setup_complete gone, the reboot below lands on exactly that wizard.

# Every archive on this box was encrypted with the master password that was in
# use when it was made, and the wizard is about to mint a brand-new one. That
# is the same boundary rotate_master_password.sh records, and for the same
# reason: without it the dashboard lists pre-reset archives as though the
# current password opens them, and the owner discovers otherwise in a
# passphrase prompt with nothing to type. The archives themselves are kept —
# restoring one is the whole point of surviving a reset.
mkdir -p /var/lib/homebrain
EPOCH_FILE="/var/lib/homebrain/backup_epoch.json"
printf '{"ts": %d, "rotated_at": "%s"}\n' \
    "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${EPOCH_FILE}.tmp" \
    && mv "${EPOCH_FILE}.tmp" "$EPOCH_FILE"
log "Backup epoch recorded — existing archives need the pre-reset password."

write_status "success" "Nuclear reset complete. Rebooting now..."

log "Syncing disks..."
sync

log "=== NUCLEAR RESET COMPLETE — REBOOTING IN 5 SECONDS ==="
sleep 5

rm -f "$LOCK_FILE"

reboot
