#!/bin/bash
set -euo pipefail

# --- Configuration ---
# distinct APP_DIR removed; we run directly from the repo structure
INSTALL_DIR="/opt/homebrain"
SERVICE_DIR="$INSTALL_DIR/src"
LOG_DIR="/var/log/homebrain"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# Boot config path detection (filesystem-based, not platform-based)
if [[ -d "/boot/firmware" ]]; then
    BOOT_CONFIG="/boot/firmware/factory_config.txt"
else
    BOOT_CONFIG="/opt/homebrain/factory_config.txt"
fi

# --- Input Validation & Mode Detection ---
if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi

# Local mode  (0-1 args): provision.sh [FACTORY_PASS]
# Remote mode (5+ args):  provision.sh <NEWT_ID> <NEWT_SECRET> <PANGOLIN_DOMAIN> <PANGOLIN_ENDPOINT> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]
if [[ $# -ge 5 ]]; then
    PROVISION_MODE="remote"
elif [[ $# -le 1 ]]; then
    PROVISION_MODE="local"
else
    echo "Usage (local mode):  $0 [FACTORY_PASS]"
    echo "Usage (remote mode): $0 <NEWT_ID> <NEWT_SECRET> <PANGOLIN_DOMAIN> <PANGOLIN_ENDPOINT> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]"
    exit 1
fi

if [[ "$PROVISION_MODE" == "remote" ]]; then
    PROV_NEWT_ID="${1}"
    PROV_NEWT_SECRET="${2}"
    PROV_PANGOLIN_DOMAIN="${3}"
    PROV_PANGOLIN_ENDPOINT="${4}"
    PROV_FACTORY_PASS="${5}"
    PROV_REGISTRAR_URL="${6:-}"
    PROV_REGISTRAR_SECRET="${7:-}"
else
    PROV_NEWT_ID=""
    PROV_NEWT_SECRET=""
    PROV_PANGOLIN_DOMAIN=""
    PROV_PANGOLIN_ENDPOINT=""
    PROV_FACTORY_PASS="${1:-}"
    PROV_REGISTRAR_URL=""
    PROV_REGISTRAR_SECRET=""
fi

# Resilience: Ensure time is correct
wait_for_time_sync

# --- 1. System Dependencies ---
echo "Installing Application Dependencies..."
install_deps_enable_docker

# --- 1b. Ensure admin user exists (Ubuntu Server doesn't ship with one) ---
ensure_admin_user

# --- 1bb. Ensure homebrain user is in docker/render/video groups ---
ensure_homebrain_user

# --- 1c. GPU-gated hardening (Vulkan drivers, firewall, grub tweaks) ---
# Detect GPU before proceeding
detect_gpu

if [[ "$HAS_GPU" == "true" ]]; then
    # Disable conflicting web servers if present
    systemctl disable --now apache2 2>/dev/null || true

    # Open firewall ports if ufw is active
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
        log_info "Opening firewall ports for HomeBrain services..."
        ufw allow 80/tcp    # Dashboard
        ufw allow 8080/tcp  # Nextcloud
        ufw allow 8123/tcp  # Home Assistant
        ufw allow 18789/tcp # OpenClaw
    fi

    # Vulkan drivers for AMD GPU (RADV via Mesa, ships in Ubuntu archive)
    log_info "Installing Vulkan drivers for AMD GPU..."
    apt-get install -y -qq mesa-vulkan-drivers libvulkan1 vulkan-tools 2>/dev/null \
        || log_warn "Vulkan driver install failed. GPU inference may not work."

    # Prevent AMD GPU runtime power management (keeps model in VRAM while idle)
    # Add amdgpu.runpm=0 and amdgpu.pg_mask=0 to GRUB if not already present
    if ! grep -q "amdgpu.runpm=0" /etc/default/grub 2>/dev/null; then
        sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 amdgpu.runpm=0 amdgpu.pg_mask=0"/' /etc/default/grub
        update-grub 2>/dev/null || true
        log_info "Disabled AMD GPU runtime PM via kernel params (requires reboot to take effect)."
    elif ! grep -q "amdgpu.pg_mask=0" /etc/default/grub 2>/dev/null; then
        sed -i 's/amdgpu.runpm=0/amdgpu.runpm=0 amdgpu.pg_mask=0/' /etc/default/grub
        update-grub 2>/dev/null || true
        log_info "Added amdgpu.pg_mask=0 to kernel params."
    fi

    # Disable GPU runtime PM immediately via sysfs (takes effect now, no reboot needed)
    GPU_PM_APPLIED=false
    for ctrl in /sys/class/drm/card*/device/power/control; do
        if [[ -f "$ctrl" ]]; then
            echo "on" > "$ctrl" 2>/dev/null && GPU_PM_APPLIED=true
        fi
    done
    if [[ "$GPU_PM_APPLIED" == "true" ]]; then
        log_info "Disabled AMD GPU runtime power management (VRAM will stay loaded)."
    fi

    # Deploy udev rule for AMD GPU runtime PM (survives hotplug/driver reload)
    cp "${SCRIPT_DIR}/../config/99-amdgpu-runpm.rules" /etc/udev/rules.d/
    udevadm control --reload-rules 2>/dev/null || true
    log_info "Deployed AMD GPU udev rule to /etc/udev/rules.d/"
fi

# --- 2. Write Factory Config ---
# Preserve any pre-existing values from a properly-imaged device. CLI args
# (when provided) take precedence; missing fields fall back to existing file
# contents. This makes re-running provision.sh idempotent — it never wipes
# the factory password baked into the OS image.
echo "Writing factory configuration (mode: ${PROVISION_MODE})..."
declare -A _FC=( [NEWT_ID]="" [NEWT_SECRET]="" [PANGOLIN_DOMAIN]="" \
                 [PANGOLIN_ENDPOINT]="" [FACTORY_PASSWORD]="" \
                 [REGISTRAR_URL]="" [REGISTRAR_SECRET]="" )
if [[ -f "$BOOT_CONFIG" ]]; then
    while IFS='=' read -r _k _v; do
        [[ -n "$_k" && "$_k" != \#* ]] && _FC[$_k]="$_v"
    done < "$BOOT_CONFIG"
fi
[[ -n "$PROV_NEWT_ID"           ]] && _FC[NEWT_ID]="$PROV_NEWT_ID"
[[ -n "$PROV_NEWT_SECRET"       ]] && _FC[NEWT_SECRET]="$PROV_NEWT_SECRET"
[[ -n "$PROV_PANGOLIN_DOMAIN"   ]] && _FC[PANGOLIN_DOMAIN]="$PROV_PANGOLIN_DOMAIN"
[[ -n "$PROV_PANGOLIN_ENDPOINT" ]] && _FC[PANGOLIN_ENDPOINT]="$PROV_PANGOLIN_ENDPOINT"
[[ -n "$PROV_FACTORY_PASS"      ]] && _FC[FACTORY_PASSWORD]="$PROV_FACTORY_PASS"
[[ -n "$PROV_REGISTRAR_URL"     ]] && _FC[REGISTRAR_URL]="$PROV_REGISTRAR_URL"
[[ -n "$PROV_REGISTRAR_SECRET"  ]] && _FC[REGISTRAR_SECRET]="$PROV_REGISTRAR_SECRET"

# Without a factory password the device is unreachable. Generate one if missing
# and surface it prominently so the operator can record it on the device label.
_GENERATED_PASS=false
if [[ -z "${_FC[FACTORY_PASSWORD]}" ]]; then
    if command -v pwgen >/dev/null 2>&1; then
        _FC[FACTORY_PASSWORD]="$(pwgen -s 16 1)"
    else
        _FC[FACTORY_PASSWORD]="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"
    fi
    _GENERATED_PASS=true
fi

cat > "$BOOT_CONFIG" <<EOF
NEWT_ID=${_FC[NEWT_ID]}
NEWT_SECRET=${_FC[NEWT_SECRET]}
PANGOLIN_DOMAIN=${_FC[PANGOLIN_DOMAIN]}
PANGOLIN_ENDPOINT=${_FC[PANGOLIN_ENDPOINT]}
FACTORY_PASSWORD=${_FC[FACTORY_PASSWORD]}
REGISTRAR_URL=${_FC[REGISTRAR_URL]}
REGISTRAR_SECRET=${_FC[REGISTRAR_SECRET]}
EOF
chmod 600 "$BOOT_CONFIG"

if [[ "$_GENERATED_PASS" == "true" ]]; then
    log_warn "═══════════════════════════════════════════════════════════"
    log_warn "GENERATED FACTORY PASSWORD: ${_FC[FACTORY_PASSWORD]}"
    log_warn "Record this on the device label — it cannot be recovered."
    log_warn "═══════════════════════════════════════════════════════════"
fi

# Store HAS_GPU for later use
update_env_var "HAS_GPU" "$HAS_GPU"

# --- 3. Setup Python Environment ---
echo "Provisioning HomeBrain Manager..."

install_python_venv_deps

# 4. Ensure scripts are executable
chmod +x "$INSTALL_DIR/scripts/"*.sh

# --- 5. Pre-load Docker Images (Hardening) ---
# We download all container images now so the user setup is fast and robust against network issues.
echo "Pre-loading Docker container images..."

# Ensure Docker is active for the pull
if ! systemctl is-active --quiet docker; then
    systemctl start docker
    sleep 5
fi

# Generate temporary .env to satisfy Compose variable substitution during pull,
# but only if a real .env doesn't already exist (idempotent re-runs).
_TEMP_ENV_CREATED=false
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cat > "$INSTALL_DIR/.env" <<EOF
# Temp Factory Env
NEXTCLOUD_DATA_DIR=/var/www/html
MASTER_PASSWORD=placeholder
PANGOLIN_DOMAIN=example.com
EOF
    _TEMP_ENV_CREATED=true
fi

# Pull images — include Pangolin profile only in remote mode
if [[ "$PROVISION_MODE" == "remote" ]]; then
    COMPOSE_PROFILES="pangolin" \
    docker compose -f "$INSTALL_DIR/docker-compose.yml" pull
else
    docker compose -f "$INSTALL_DIR/docker-compose.yml" pull
fi

# Cleanup temp env only if we created it; preserve a real one across re-runs
if [[ "$_TEMP_ENV_CREATED" == "true" ]]; then
    rm "$INSTALL_DIR/.env"
fi

# --- 5. Install Service ---
echo "Configuring Systemd Service..."

# Copy the service file
SERVICE_FILE="$INSTALL_DIR/config/homebrain-manager.service"

if [ -f "$SERVICE_FILE" ]; then
    cp "$SERVICE_FILE" /etc/systemd/system/

    systemctl daemon-reload
    systemctl enable --now homebrain-manager.service
else
    echo "ERROR: Service file not found at $SERVICE_FILE"
    exit 1
fi

# Deploy and enable sleep inhibitor service
cp "${SCRIPT_DIR}/../config/inhibit-sleep.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now inhibit-sleep.service 2>/dev/null \
    && log_info "Sleep inhibitor service enabled." \
    || log_warn "Failed to enable sleep inhibitor service."

echo "HomeBrain Provisioning Complete."
echo "======================================================="
echo "   PROVISIONING COMPLETE"
echo "======================================================="
echo "   Device is ready for first boot."
echo "   Password will be generated during deployment."
echo "======================================================="
log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_info "ACTION REQUIRED: Set BIOS 'Restore on AC Power Loss' → 'Power On'"
log_info "This ensures HomeBrain auto-starts after a power outage."
log_info "Location: BIOS → Power Management → AC Power Recovery"
log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
