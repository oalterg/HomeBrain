#!/bin/bash
set -euo pipefail

# --- Configuration ---
# distinct APP_DIR removed; we run directly from the repo structure
INSTALL_DIR="/opt/homebrain"
SERVICE_DIR="$INSTALL_DIR/src"
BOOT_CONFIG="/boot/firmware/factory_config.txt"
LOG_DIR="/var/log/homebrain"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# --- Input Validation ---
if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi
if [ "$#" -lt 5 ]; then echo "Usage: $0 <ID> <SECRET> <MAIN_DOMAIN> <PAN_EP> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]"; exit 1; fi

# Resilience: Ensure time is correct
wait_for_time_sync

# --- 1. System Dependencies ---
echo "Installing Application Dependencies..."
install_deps_enable_docker

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
