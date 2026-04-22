#!/bin/bash

# --- Global Configuration ---
export INSTALL_DIR="/opt/homebrain"
export LOG_DIR="/var/log/homebrain"
export ENV_FILE="$INSTALL_DIR/.env"
export COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
export OVERRIDE_FILE="$INSTALL_DIR/docker-compose.override.yml"
export BACKUP_MOUNTDIR="/mnt/backup"

# --- Canonical HomeBrain OS User ---
export HOMEBRAIN_USER="homebrain"
export HOMEBRAIN_HOME="/home/${HOMEBRAIN_USER}"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# --- Logging Helpers ---
log_info() { echo "[INFO] $1" >&2; }
log_warn() { echo "[WARN] $1" >&2; }
log_error() { echo "[ERROR] $1" >&2; }
die() { log_error "$1" >&2; exit 1; }

# --- GPU Detection ---
detect_gpu() {
  # Layer 1: DRM render node (works for AMD/Nvidia/Intel on Linux)
  if ls /dev/dri/renderD* &>/dev/null 2>&1; then
    HAS_GPU=true; export HAS_GPU; return 0
  fi
  # Layer 2: sysfs DRM
  if ls /sys/class/drm/render* &>/dev/null 2>&1; then
    HAS_GPU=true; export HAS_GPU; return 0
  fi
  # Layer 3: lspci VGA/3D/Display controller
  if command -v lspci &>/dev/null && lspci 2>/dev/null | grep -qiE "VGA|3D|Display"; then
    HAS_GPU=true; export HAS_GPU; return 0
  fi
  HAS_GPU=false; export HAS_GPU
}
detect_gpu

# --- User Management ---
# Verify the homebrain system user exists and is in the required groups.
# The user is pre-provisioned on the OS image; this function is a guard only.
ensure_homebrain_user() {
    if ! id -u "${HOMEBRAIN_USER}" >/dev/null 2>&1; then
        die "System user '${HOMEBRAIN_USER}' does not exist. Please provision the OS image correctly."
    fi
    # Ensure homebrain is in required groups (idempotent)
    for grp in docker render video; do
        if getent group "$grp" >/dev/null 2>&1; then
            usermod -aG "$grp" "${HOMEBRAIN_USER}" 2>/dev/null || true
        fi
    done
}

# --- Admin user creation (Ubuntu x86 doesn't ship with a default user) ---
ensure_admin_user() {
    if id -u admin >/dev/null 2>&1; then
        log_info "admin user already exists."
        return 0
    fi
    log_info "Creating admin user for Ubuntu x86..."
    useradd -m -s /bin/bash admin
    mkdir -p /home/admin/.ssh
    chmod 700 /home/admin/.ssh
    # Add to render/video for GPU access, docker for container management
    for grp in render video docker; do
        if getent group "$grp" >/dev/null 2>&1; then
            usermod -aG "$grp" admin 2>/dev/null || true
        fi
    done
}

# --- Environment Loading ---
load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        source "$ENV_FILE"
        set +a
    else
        die "Environment file ($ENV_FILE) not found."
    fi
}

# --- Resilience Helpers ---
check_internet() {
    ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1
}

wait_for_time_sync() {
    # Robustness: Block setup until system time is valid (Year >= 2023)
    # Critical for SSL certificates, Oauth, and TOTP.
    if [[ $(date +%Y) -lt 2026 ]]; then
        log_info "System time invalid ($(date)). Waiting for NTP sync..."
        # Attempt to force systemd-timesyncd
        systemctl restart systemd-timesyncd || true
        
        local retries=0
        while [[ $(date +%Y) -lt 2026 ]]; do
            sleep 5
            ((retries++))
            if [[ $retries -gt 24 ]]; then log_warn "Time sync timed out. SSL/OAuth may fail."; break; fi
        done
        log_info "Time synchronized: $(date)"
    fi
}


# --- Configuration Helpers ---
update_env_var() {
    local key="$1"
    local value="$2"
    
    if [[ -f "$ENV_FILE" ]]; then
        # If key exists, replace it
        if grep -q "^${key}=" "$ENV_FILE"; then
            # Escape value for sed (basic safety for URLs/domains)
            local safe_val
            safe_val=$(printf '%s\n' "$value" | sed -e 's/[\/&]/\\&/g')
            sed -i "s|^${key}=.*|${key}='${safe_val}'|" "$ENV_FILE"
        else
            # If key missing, append it
            echo "${key}='${value}'" >> "$ENV_FILE"
        fi
    else
        log_warn ".env file not found, creating new one."
        echo "${key}='${value}'" > "$ENV_FILE"
    fi
}

# --- Docker Helpers ---
# Helper to get all active compose files
get_compose_args() {
    local args="-f $COMPOSE_FILE"
    if [[ -f "$OVERRIDE_FILE" ]]; then
        args="$args -f $OVERRIDE_FILE"
    fi
    echo "$args"
}

get_nc_cid() {
    docker compose $(get_compose_args) ps -a -q nextcloud 2>/dev/null || true
}

get_ha_cid() {
    docker compose $(get_compose_args) ps -a -q homeassistant 2>/dev/null || true
}

get_nc_db_cid() {
    docker compose $(get_compose_args) ps -a -q db 2>/dev/null || true
}

is_stack_running() {
    local nc_cid=$(get_nc_cid)
    local ha_cid=$(get_ha_cid)
    # Returns true only if both Nextcloud and Home Assistant container IDs are found and are running
    [[ -n "$nc_cid" ]] && [[ $(docker inspect -f '{{.State.Running}}' "$nc_cid" 2>/dev/null) == "true" ]] && \
    [[ -n "$ha_cid" ]] && [[ $(docker inspect -f '{{.State.Running}}' "$ha_cid" 2>/dev/null) == "true" ]]
}

# --- Tunnel Profiles Helper ---
get_tunnel_profiles() {
    local profiles=""
    # 1. Sanitize Inputs (Trim Whitespace) to prevent false positives
    local p_endpoint="${PANGOLIN_ENDPOINT:-}"; p_endpoint="${p_endpoint//[[:space:]]/}"
    local p_id="${NEWT_ID:-}"; p_id="${p_id//[[:space:]]/}"
    local p_secret="${NEWT_SECRET:-}"; p_secret="${p_secret//[[:space:]]/}"
    local cf_nc_token="${CF_TOKEN_NC:-}"; cf_nc_token="${cf_nc_token//[[:space:]]/}"
    local cf_ha_token="${CF_TOKEN_HA:-}"; cf_ha_token="${cf_ha_token//[[:space:]]/}"

    # 2. Determine Mode (custom Cloudflare prioritized over Pangolin)
    # We enforce mutual exclusivity: If Cloudflare tokens are provided, we ignore Pangolin tokens.

    if [[ -n "$cf_nc_token" ]] || [[ -n "$cf_ha_token" ]]; then
        # --- Cloudflare Mode ---
        if [[ -n "$cf_nc_token" ]]; then
            profiles="${profiles} --profile cloudflare-nc"
        fi
        if [[ -n "$cf_ha_token" ]]; then
            profiles="${profiles} --profile cloudflare-ha"
        fi
    elif [[ -n "$p_endpoint" ]] && [[ -n "$p_id" ]] && [[ -n "$p_secret" ]]; then
        # --- Pangolin Mode ---
        profiles="--profile pangolin"
    else
        log_info "No complete tunnel configuration found. Deploying local-only."
    fi

    # Trim leading space if any
    profiles="${profiles#" "}"

    echo "${profiles}"
}

# Returns 0 (true) when running in local/LAN-only mode.
# Logic: local if Pangolin not provisioned (credentials absent), OR if user explicitly
# set DEPLOYMENT_MODE=local to opt out despite having credentials.
# When Pangolin IS provisioned, tunnel is on by default (DEPLOYMENT_MODE defaults to remote).
is_local_mode() {
    # No Pangolin credentials at all — always local regardless of DEPLOYMENT_MODE
    [[ -z "${NEWT_ID:-}" || -z "${NEWT_SECRET:-}" || -z "${PANGOLIN_DOMAIN:-}" ]] && return 0
    # Credentials present but user explicitly opted out
    [[ "${DEPLOYMENT_MODE:-remote}" == "local" ]] && return 0
    return 1
}

wait_for_healthy() {
    local service_name="$1"
    local timeout_seconds="$2"
    local container_id

    log_info "Waiting for $service_name to become healthy..."
    
    # Retry finding container ID
    local retries=10
    while [[ $retries -gt 0 ]]; do
        container_id=$(docker compose $(get_compose_args) ps -q "$service_name" 2>/dev/null)
        if [[ -n "$container_id" ]]; then break; fi
        sleep 2
        ((retries--))
    done

    [[ -z "$container_id" ]] && return 1

    local end_time=$((SECONDS + timeout_seconds))
    while [ $SECONDS -lt $end_time ]; do
        local status
        status=$(docker inspect --format="{{if .State.Health}}{{.State.Health.Status}}{{end}}" "$container_id" 2>/dev/null || echo "unknown")
        if [ "$status" == "healthy" ]; then
            log_info "✅ $service_name is healthy."
            return 0
        fi
        sleep 3
    done
    log_error "❌ $service_name failed health check."
    return 1
}

wait_for_apt_lock() {
    local lock_files=("/var/lib/dpkg/lock" "/var/lib/dpkg/lock-frontend" "/var/lib/apt/lists/lock")
    for lock in "${lock_files[@]}"; do
        while fuser "$lock" >/dev/null 2>&1; do
            log_info "Waiting for apt lock ($lock)..."
            sleep 3
        done
    done
}

install_deps_enable_docker() {
    # Offline Fallback: If offline but docker exists, skip apt to prevent crash
    if ! check_internet; then
        if command -v docker >/dev/null; then
            log_warn "Offline mode detected. Skipping apt updates (Docker already installed)."
            return
        else
            log_warn "No internet and Docker not found. Proceeding with apt (may fail)..."
        fi
    fi

    # --- 0. Install Dependencies ---
    log_info "Installing dependencies"
    wait_for_apt_lock
    local common_pkgs="ca-certificates gnupg lsb-release cron gpg rsync python3-flask python3-dotenv python3-requests python3-pip python3-venv jq moreutils pwgen git parted"
    apt-get install -y -qq $common_pkgs

    # Install Google Chrome for OpenClaw browser tool (x86 only, non-fatal)
    # Uses deb package instead of snap to avoid confinement issues on headless servers
    if [[ "$HAS_GPU" == "true" ]]; then
        if ! command -v google-chrome-stable >/dev/null 2>&1; then
            log_info "Installing Google Chrome for headless browsing..."
            wget -q -O /tmp/google-chrome.deb \
                "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
                && apt-get install -y -qq /tmp/google-chrome.deb \
                && rm -f /tmp/google-chrome.deb \
                || log_warn "Chrome install failed. OpenClaw browser tool may not work."
        fi
        if command -v google-chrome-stable >/dev/null 2>&1; then
            log_info "Chrome available for headless browsing."
        else
            log_warn "Chrome not available. OpenClaw browser tool will not work."
        fi
    fi
    apt-get update -qq

    # Docker setup
    if ! [ -f /etc/apt/keyrings/docker.gpg ]; then
        mkdir -p /etc/apt/keyrings
        local os_id
        os_id=$(. /etc/os-release && echo "$ID")  # "debian" or "ubuntu"
        curl -fsSL "https://download.docker.com/linux/${os_id}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${os_id} $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
        apt-get update -y -qq
    fi
    
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    log_info "Starting docker service"
    systemctl enable --now docker
}

install_python_venv_deps(){
    # Install Python dependencies in venv
    VENV_DIR="$INSTALL_DIR/venv"
    
    # Optimization: If venv exists and seems valid, skip pip (Offline Safe)
    if [ -f "$VENV_DIR/bin/activate" ] && [ -d "$VENV_DIR/lib" ]; then
        if ! check_internet; then
            log_info "Offline: Using existing venv."
            return
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtualenv..."
        python3 -m venv "$VENV_DIR"
    fi
    
    # Use direct path to pip to avoid 'source' issues in strict mode (set -u)
    local venv_pip="$VENV_DIR/bin/pip"
    # Upgrade pip in venv
    "$venv_pip" install --upgrade pip
    
    # Install requirements (no conflicts)
    if [ -f "$INSTALL_DIR/requirements.txt" ]; then
        echo "Installing Python dependencies..."
        "$venv_pip" install -r "$INSTALL_DIR/requirements.txt"
    fi
}

# --- Maintenance Mode ---
set_maintenance_mode() {
    local mode="$1" # --on or --off
    local nc_cid
    nc_cid=$(get_nc_cid)
    
    if [[ -z "$nc_cid" ]]; then return 1; fi
    
    log_info "Setting Nextcloud maintenance mode: $mode"
    docker exec -u www-data "$nc_cid" php occ maintenance:mode "$mode" || true
}

# Configures Trusted Proxies in Home Assistant configuration.yaml
configure_ha_proxy_settings() {
    local subnet="$1"
    local cid="$2"

    log_info "Configuring Home Assistant trusted proxies for subnet: $subnet"

    docker exec "$cid" sh -c "
        CONF='/config/configuration.yaml'
        # 1. Check if the subnet is already trusted
        if grep -Fq '$subnet' \"\$CONF\"; then
            echo 'Subnet already trusted.'
        else
            # 2. Check if trusted_proxies block exists
            if grep -q 'trusted_proxies:' \"\$CONF\"; then
                # Append to existing list
                sed -i '/trusted_proxies:/a \    - $subnet' \"\$CONF\"
            # 3. Check if http block exists but no proxies
            elif grep -q '^http:' \"\$CONF\"; then
                sed -i '/^http:/a \  use_x_forwarded_for: true\n  trusted_proxies:\n    - $subnet' \"\$CONF\"
            # 4. No http block at all
            else
                echo '
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - $subnet
' >> \"\$CONF\"
            fi
        fi
    "
}

configure_nc_ha_proxy_settings() {
    log_info "Configuring trusted proxies for Docker Subnet..."
    local nc_cid=$(get_nc_cid)
    local ha_cid=$(get_ha_cid)
    
    # Get Docker Bridge Subnet
    local subnet
    # Try to find the network used by nextcloud
    if [[ -n "$nc_cid" ]]; then
        local net_name=$(docker inspect "$nc_cid" --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')
        if [[ -n "$net_name" ]]; then
            subnet=$(docker network inspect "$net_name" --format='{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
        fi
    fi
    
    # Fallback default if detection fails
    if [[ -z "$subnet" ]]; then
        subnet="172.16.0.0/12"
    fi
    log_info "Detected Docker Subnet: $subnet"

    # 1. Update Nextcloud Trusted Proxies
    if [[ -n "$nc_cid" ]]; then
        if is_local_mode; then
            # Local mode: HTTP only, trust LAN addresses, no tunnel domain required
            local lan_ip
            lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
            docker exec --user www-data "$nc_cid" php occ config:system:set overwriteprotocol --value=http || die "Failed to set overwriteprotocol."
            docker exec --user www-data "$nc_cid" php occ config:system:set overwrite.cli.url --value="http://homebrain.local:8080" || true
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 1 --value="localhost" || die "Failed to set trusted_domains localhost."
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 2 --value="homebrain.local" || die "Failed to set trusted_domains homebrain.local."
            if [[ -n "$lan_ip" ]]; then
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 3 --value="$lan_ip" || die "Failed to set trusted_domains LAN IP."
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 4 --value="${lan_ip}:8080" || true
            fi
            # If an explicit trusted domain was set anyway, honour it at slot 5
            if [[ -n "${NEXTCLOUD_TRUSTED_DOMAINS:-}" ]]; then
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 5 --value="$NEXTCLOUD_TRUSTED_DOMAINS" || true
            fi
        else
            # Remote mode: HTTPS via tunnel, use configured domain
            docker exec --user www-data "$nc_cid" php occ config:system:set overwriteprotocol --value=https || die "Failed to set overwriteprotocol."
            docker exec --user www-data "$nc_cid" php occ config:system:set overwrite.cli.url --value="https://${NEXTCLOUD_TRUSTED_DOMAINS}" || true
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 1 --value="$NEXTCLOUD_TRUSTED_DOMAINS" || die "Failed to set trusted_domains 1."
        fi
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 0 --value="$TRUSTED_PROXIES_0" || die "Failed to set trusted_proxies 0."
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 1 --value="$TRUSTED_PROXIES_1" || die "Failed to set trusted_proxies 1."
        # Use index 10 to avoid conflict with existing static ones
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 10 --value="$subnet" || die "Failed to set trusted_proxies 10."
        # Also ensure localhost is trusted
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 11 --value="127.0.0.1" || die "Failed to set trusted_proxies 11."
    fi

    # 2. Update Home Assistant Trusted Proxies
    if [[ -n "$ha_cid" ]]; then
        configure_ha_proxy_settings "$subnet" "$ha_cid"
    fi
}

configure_nextcloud_redis() {
    local nc_cid=$(get_nc_cid)
    
    if [[ -z "$nc_cid" ]]; then 
        log_warn "Nextcloud container not found. Skipping Redis config."
        return 1
    fi

    log_info "Configuring Redis for Nextcloud..."
    
    # We use '|| true' on some commands to prevent a hard failure if the config is already set,
    # though 'occ config:system:set' is generally idempotent.
    
    # 1. Configure Connection Details
    docker exec --user www-data "$nc_cid" php occ config:system:set redis host --value="redis" || return 1
    docker exec --user www-data "$nc_cid" php occ config:system:set redis port --value=6379 --type=integer
    
    # 2. Configure Caching Backends
    # Distributed cache (Redis)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.distributed --value="\OC\Memcache\Redis"
    # Locking (Redis is much faster than DB locking)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.locking --value="\OC\Memcache\Redis"
    # Local Cache (APCu is faster for local, but Redis is acceptable if APCu is missing. We prefer APCu)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.local --value="\OC\Memcache\APCu"

    log_info "Redis configuration applied successfully."
}
