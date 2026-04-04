#!/bin/bash
set -euo pipefail

# --- Configuration ---
# distinct APP_DIR removed; we run directly from the repo structure
INSTALL_DIR="/opt/homebrain"
SERVICE_DIR="$INSTALL_DIR/src"
LOG_DIR="/var/log/homebrain"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# Platform-conditional boot config path
if [[ "$HB_PLATFORM" == "rpi5" ]]; then
    BOOT_CONFIG="/boot/firmware/factory_config.txt"
else
    BOOT_CONFIG="/opt/homebrain/factory_config.txt"
fi

# --- Input Validation ---
if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi
if [ "$#" -lt 5 ]; then echo "Usage: $0 <ID> <SECRET> <MAIN_DOMAIN> <PAN_EP> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]"; exit 1; fi

# Resilience: Ensure time is correct
wait_for_time_sync

# --- 1. System Dependencies ---
echo "Installing Application Dependencies..."
install_deps_enable_docker

# --- 1b. Ensure admin user exists (Ubuntu Server doesn't ship with one) ---
ensure_admin_user

# --- 1c. Platform-specific hardening ---
if [[ "$HB_PLATFORM" == "x86_ubuntu" ]]; then
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
echo "Writing factory configuration..."
cat > "$BOOT_CONFIG" <<EOF
NEWT_ID=${1}
NEWT_SECRET=${2}
PANGOLIN_DOMAIN=${3}
PANGOLIN_ENDPOINT=${4}
FACTORY_PASSWORD=${5}
REGISTRAR_URL=${6:-}
REGISTRAR_SECRET=${7:-}
EOF
chmod 600 "$BOOT_CONFIG"

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

# Pull Pangolin to ensure images are present
COMPOSE_PROFILES="pangolin" \
docker compose -f "$INSTALL_DIR/docker-compose.yml" pull

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
