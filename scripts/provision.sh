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
    grub_file="/etc/default/grub"
    if [[ -f "$grub_file" ]] && ! grep -q "amdgpu.runpm=0" "$grub_file"; then
        log_info "Disabling AMD GPU runtime power management (amdgpu.runpm=0)..."
        sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 amdgpu.runpm=0"/' "$grub_file"
        update-grub 2>/dev/null || log_warn "update-grub failed. Kernel parameter may require manual setup."
    fi
fi

# --- 2. Write Factory Config ---
echo "Writing factory configuration (mode: ${PROVISION_MODE})..."
cat > "$BOOT_CONFIG" <<EOF
NEWT_ID=${PROV_NEWT_ID}
NEWT_SECRET=${PROV_NEWT_SECRET}
PANGOLIN_DOMAIN=${PROV_PANGOLIN_DOMAIN}
PANGOLIN_ENDPOINT=${PROV_PANGOLIN_ENDPOINT}
FACTORY_PASSWORD=${PROV_FACTORY_PASS}
REGISTRAR_URL=${PROV_REGISTRAR_URL}
REGISTRAR_SECRET=${PROV_REGISTRAR_SECRET}
EOF
chmod 600 "$BOOT_CONFIG"

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

# Generate temporary .env to satisfy Compose variable substitution during pull
cat > "$INSTALL_DIR/.env" <<EOF
# Temp Factory Env
NEXTCLOUD_DATA_DIR=/var/www/html
MASTER_PASSWORD=placeholder
PANGOLIN_DOMAIN=example.com
EOF

# Pull images — include Pangolin profile only in remote mode
if [[ "$PROVISION_MODE" == "remote" ]]; then
    COMPOSE_PROFILES="pangolin" \
    docker compose -f "$INSTALL_DIR/docker-compose.yml" pull
else
    docker compose -f "$INSTALL_DIR/docker-compose.yml" pull
fi

# Cleanup temp env so User Setup generates a fresh secure one
rm "$INSTALL_DIR/.env"

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

echo "HomeBrain Provisioning Complete."
echo "======================================================="
echo "   PROVISIONING COMPLETE"
echo "======================================================="
echo "   Device is ready for first boot."
echo "   Password will be generated during deployment."
echo "======================================================="
