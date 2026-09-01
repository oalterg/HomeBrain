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

# --- Input Validation & Argument Parsing ---
if [[ $EUID -ne 0 ]]; then echo "Run as root."; exit 1; fi

# Two invocation styles, both supported:
#
#   Positional (back-compat):
#     local:  provision.sh [FACTORY_PASS]
#     remote: provision.sh <NEWT_ID> <NEWT_SECRET> <PANGOLIN_DOMAIN> <PANGOLIN_ENDPOINT> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]
#
#   Named flags (idempotent re-provision / tunnel repoint). Any value not given
#   is inherited from the existing factory_config, so repointing a live box to a
#   new tunnel is a one-liner:
#     provision.sh --newt-id ID --newt-secret SECRET --domain DOMAIN \
#                  [--endpoint URL] [--factory-pass PASS] \
#                  [--registrar-url URL] [--registrar-secret SECRET] [--local] [--no-apply]
#
# Remote vs local is decided in section 2, AFTER merging with the existing
# factory_config: remote iff NEWT_ID, NEWT_SECRET and PANGOLIN_DOMAIN are all
# present (and --local was not passed).
PROV_NEWT_ID="" ; PROV_NEWT_SECRET="" ; PROV_PANGOLIN_DOMAIN=""
PROV_PANGOLIN_ENDPOINT="" ; PROV_FACTORY_PASS=""
PROV_REGISTRAR_URL="" ; PROV_REGISTRAR_SECRET=""
FORCE_LOCAL=false
APPLY_REDEPLOY=true   # on an already-set-up box, redeploy so the repoint goes live

if [[ "${1:-}" == --* ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --newt-id)          PROV_NEWT_ID="${2:?--newt-id needs a value}"; shift 2;;
            --newt-secret)      PROV_NEWT_SECRET="${2:?--newt-secret needs a value}"; shift 2;;
            --domain)           PROV_PANGOLIN_DOMAIN="${2:?--domain needs a value}"; shift 2;;
            --endpoint)         PROV_PANGOLIN_ENDPOINT="${2:?--endpoint needs a value}"; shift 2;;
            --factory-pass)     PROV_FACTORY_PASS="${2:?--factory-pass needs a value}"; shift 2;;
            --registrar-url)    PROV_REGISTRAR_URL="${2:?--registrar-url needs a value}"; shift 2;;
            --registrar-secret) PROV_REGISTRAR_SECRET="${2:?--registrar-secret needs a value}"; shift 2;;
            --local)            FORCE_LOCAL=true; shift;;
            --no-apply)         APPLY_REDEPLOY=false; shift;;
            *) echo "Unknown flag: $1"; exit 1;;
        esac
    done
elif [[ $# -ge 5 ]]; then
    PROV_NEWT_ID="${1}"
    PROV_NEWT_SECRET="${2}"
    PROV_PANGOLIN_DOMAIN="${3}"
    PROV_PANGOLIN_ENDPOINT="${4}"
    PROV_FACTORY_PASS="${5}"
    PROV_REGISTRAR_URL="${6:-}"
    PROV_REGISTRAR_SECRET="${7:-}"
elif [[ $# -le 1 ]]; then
    PROV_FACTORY_PASS="${1:-}"
else
    echo "Usage (local):  $0 [FACTORY_PASS]"
    echo "Usage (remote): $0 <NEWT_ID> <NEWT_SECRET> <PANGOLIN_DOMAIN> <PANGOLIN_ENDPOINT> <FACTORY_PASS> [REGISTRAR_URL] [REGISTRAR_SECRET]"
    echo "Usage (flags):  $0 --newt-id ID --newt-secret SECRET --domain DOMAIN [--endpoint URL] [--factory-pass PASS] [--local] [--no-apply]"
    exit 1
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

# --- 1c. Host prep + driver-specific GPU hardening ---
detect_platform
log_info "Platform: ${HB_PLATFORM_TAG} (driver=${HB_GPU_DRIVER}, memory=${HB_GPU_MEMORY}, has_gpu=${HAS_GPU})"
emit_platform_json

# Neither of these is GPU-related. They sat behind the HAS_GPU gate, which left
# a no-GPU HomeCloud box with its firewall closed and a stray apache2 holding :80.
systemctl disable --now apache2 2>/dev/null || true

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
    log_info "Opening firewall ports for HomeBrain services..."
    ufw allow 80/tcp    # Dashboard
    ufw allow 8080/tcp  # Nextcloud
    ufw allow 8123/tcp  # Home Assistant
    # No 18789 rule: the OpenClaw gateway binds loopback only and is
    # reached through the manager's authenticated proxy.
fi

harden_gpu

# --- 2. Write Factory Config ---
# Preserve any pre-existing values from a properly-imaged device. CLI args
# (when provided) take precedence; missing fields fall back to existing file
# contents. This makes re-running provision.sh idempotent — it never wipes
# the factory password baked into the OS image, and an operator can repoint the
# tunnel by supplying only the changed fields.
declare -A _FC=( [NEWT_ID]="" [NEWT_SECRET]="" [PANGOLIN_DOMAIN]="" \
                 [PANGOLIN_ENDPOINT]="" [FACTORY_PASSWORD]="" \
                 [REGISTRAR_URL]="" [REGISTRAR_SECRET]="" )
if [[ -f "$BOOT_CONFIG" ]]; then
    # Split on the first '=' only — NEWT_SECRET / FACTORY_PASSWORD /
    # REGISTRAR_SECRET are generated values that can legitimately end in '=',
    # which `IFS='=' read` would eat as a delimiter. See restore.sh for the
    # same bug caught in the act on HOMEBRAIN_EMAIL_KEY.
    while read -r _line || [[ -n "$_line" ]]; do
        [[ -z "$_line" || "$_line" == \#* || "$_line" != *=* ]] && continue
        _k="${_line%%=*}"
        [[ -n "$_k" ]] && _FC[$_k]="${_line#*=}"
    done < "$BOOT_CONFIG"
fi
[[ -n "$PROV_NEWT_ID"           ]] && _FC[NEWT_ID]="$PROV_NEWT_ID"
[[ -n "$PROV_NEWT_SECRET"       ]] && _FC[NEWT_SECRET]="$PROV_NEWT_SECRET"
[[ -n "$PROV_PANGOLIN_DOMAIN"   ]] && _FC[PANGOLIN_DOMAIN]="$PROV_PANGOLIN_DOMAIN"
[[ -n "$PROV_PANGOLIN_ENDPOINT" ]] && _FC[PANGOLIN_ENDPOINT]="$PROV_PANGOLIN_ENDPOINT"
[[ -n "$PROV_FACTORY_PASS"      ]] && _FC[FACTORY_PASSWORD]="$PROV_FACTORY_PASS"
[[ -n "$PROV_REGISTRAR_URL"     ]] && _FC[REGISTRAR_URL]="$PROV_REGISTRAR_URL"
[[ -n "$PROV_REGISTRAR_SECRET"  ]] && _FC[REGISTRAR_SECRET]="$PROV_REGISTRAR_SECRET"

# Operators paste the tunnel domain as a URL ("https://home.example.com/").
# Every public hostname is derived by prefixing labels onto this value
# (nc.<domain>), so a scheme or path here poisons every trusted domain and
# Vaultwarden's DOMAIN. Reduce to the bare hostname; mirrors app.py's
# sanitize_domain. Applied after the merge so it also cleans a poisoned
# value inherited from an existing factory_config.
_FC[PANGOLIN_DOMAIN]="${_FC[PANGOLIN_DOMAIN]#*://}"
_FC[PANGOLIN_DOMAIN]="${_FC[PANGOLIN_DOMAIN]%%[/?#]*}"
_FC[PANGOLIN_DOMAIN]="${_FC[PANGOLIN_DOMAIN]%%:*}"

# Decide deployment mode from the EFFECTIVE (merged) config, not raw argc — this
# lets `--domain ...` alone (endpoint/creds inherited from factory_config) still
# resolve to remote.
if [[ "$FORCE_LOCAL" == "true" ]]; then
    PROVISION_MODE="local"
elif [[ -n "${_FC[NEWT_ID]}" && -n "${_FC[NEWT_SECRET]}" && -n "${_FC[PANGOLIN_DOMAIN]}" ]]; then
    PROVISION_MODE="remote"
else
    PROVISION_MODE="local"
fi
echo "Writing factory configuration (mode: ${PROVISION_MODE})..."

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

# HAS_GPU is re-detected on every common.sh source (detect_gpu), and app.py
# probes /dev/dri as fallback — no need to persist it in .env. Writing it here
# would create .env before the setup wizard runs and prevent the template copy.

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

# Deploy and enable the health check timer (proactive owner notifications)
cp "${SCRIPT_DIR}/../config/homebrain-health.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/../config/homebrain-health.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homebrain-health.timer 2>/dev/null \
    && log_info "Health check timer enabled." \
    || log_warn "Failed to enable health check timer."

# HA watchers: Telegram ping on Home Assistant state_changed. Condition in
# the unit skips start until OpenClaw exists (GPU boxes).
cp "${SCRIPT_DIR}/../config/homebrain-ha-watch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homebrain-ha-watch.service 2>/dev/null \
    && log_info "HA watcher service enabled." \
    || log_warn "HA watcher service not started (OpenClaw not present yet is OK)."

# Rotate /var/log/homebrain. Without this it grows for the life of the box.
cp "${SCRIPT_DIR}/../config/logrotate-homebrain" /etc/logrotate.d/homebrain
chmod 644 /etc/logrotate.d/homebrain
if logrotate --debug /etc/logrotate.d/homebrain >/dev/null 2>&1; then
    log_info "Log rotation configured."
else
    log_warn "logrotate rejected /etc/logrotate.d/homebrain — logs will grow unbounded."
fi

# Resume timer for interrupted off-site copies. Harmless when off-site is not
# configured — offsite_resume exits immediately on OFFSITE_ENABLED=false.
cp "${SCRIPT_DIR}/../config/homebrain-offsite.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/../config/homebrain-offsite.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homebrain-offsite.timer 2>/dev/null \
    && log_info "Off-site resume timer enabled." \
    || log_warn "Failed to enable off-site resume timer."

# --- 6. OpenClaw integration scaffold ---
# Make sure /home/homebrain/.openclaw/ exists with the right ownership before
# the dashboard starts wiring MCP servers into it. Idempotent.
mkdir -p "${HOMEBRAIN_HOME:-/home/homebrain}/.openclaw"
chown -R homebrain:homebrain "${HOMEBRAIN_HOME:-/home/homebrain}/.openclaw" 2>/dev/null || true
chmod 700 "${HOMEBRAIN_HOME:-/home/homebrain}/.openclaw" 2>/dev/null || true

# Audit log directory. Owned by root (the manager service writes here);
# MCP servers running as the homebrain user fall back to stderr if they
# can't write. Setting it group-writable for the homebrain group lets the
# stdio MCP subprocesses append directly.
mkdir -p /var/log/homebrain
chgrp homebrain /var/log/homebrain 2>/dev/null || true
chmod 2775 /var/log/homebrain 2>/dev/null || true

# --- 7. Idempotent tunnel repoint (already-provisioned boxes only) ---
# Everything above only writes the *factory* record (factory_config.txt). The
# live stack reads tunnel creds + trusted domains from .env, applied to the
# containers by redeploy_tunnels.sh. On a box that has already completed setup
# (.setup_complete present) we propagate the new tunnel identity into .env and
# redeploy, so a SINGLE provision.sh run fully repoints the box to the new
# domain while keeping all data.
#
# Data-safety contract: this path never touches the nextcloud/vault data
# bind-mounts and uses redeploy_tunnels.sh, which has no wipe logic. deploy.sh's
# fresh-install wipe branch is gated on the ABSENCE of .setup_complete and is
# never invoked here. On a fresh box (no .setup_complete) we skip this entirely
# — the setup wizard drives the first deploy.
if [[ "$PROVISION_MODE" == "remote" && -f "$INSTALL_DIR/.setup_complete" && -f "$ENV_FILE" ]]; then
    _dom="${_FC[PANGOLIN_DOMAIN]}"
    log_info "Already-provisioned box — propagating new tunnel identity into .env (domain: ${_dom})."
    update_env_var "DEPLOYMENT_MODE"           "remote"
    update_env_var "NEWT_ID"                   "${_FC[NEWT_ID]}"
    update_env_var "NEWT_SECRET"               "${_FC[NEWT_SECRET]}"
    update_env_var "PANGOLIN_ENDPOINT"         "${_FC[PANGOLIN_ENDPOINT]}"
    update_env_var "PANGOLIN_DOMAIN"           "$_dom"
    update_env_var "MANAGER_DOMAIN"            "$_dom"
    update_env_var "NEXTCLOUD_TRUSTED_DOMAINS" "nc.$_dom"
    update_env_var "HA_TRUSTED_DOMAINS"        "ha.$_dom"
    update_env_var "VAULT_TRUSTED_DOMAINS"     "vault.$_dom"
    update_env_var "VAULT_DOMAIN"              "https://vault.$_dom"

    if [[ "$APPLY_REDEPLOY" == "true" ]]; then
        log_info "Redeploying tunnel to bring the new domain live (data-safe)..."
        if bash "$INSTALL_DIR/scripts/redeploy_tunnels.sh"; then
            log_info "Tunnel redeploy complete — services now published under ${_dom}."
        else
            log_warn "redeploy_tunnels.sh reported issues; inspect $LOG_DIR/main_setup.log."
        fi
        # Confirm the new tunnel actually came up, then remind the operator of the
        # Pangolin org-side resources only they can configure (with correct targets).
        log_warn "Remote access now flips to the new tunnel."
        verify_newt_connected || true
        print_pangolin_resource_guide "${_dom}"
    else
        log_info "--no-apply: .env updated but tunnel not redeployed. Run scripts/redeploy_tunnels.sh to apply."
    fi
fi

echo "HomeBrain Provisioning Complete."
echo "======================================================="
echo "   PROVISIONING COMPLETE"
echo "======================================================="
echo "   Setup wizard is running. Open http://<server-ip> in a browser."
# Repeat the generated password HERE, not only where it was minted. It is
# minted a few hundred lines of apt/venv/docker output before this point, on
# stderr, so on a `curl | sudo bash` install the one credential needed to
# continue had scrolled well off the screen by the time the operator was told
# to go and find it.
if [[ "$_GENERATED_PASS" == "true" ]]; then
    echo "   Log in with this factory password:"
    echo ""
    echo "       ${_FC[FACTORY_PASSWORD]}"
    echo ""
    echo "   Record it on the device label — it cannot be recovered."
else
    echo "   Log in with the factory password set for this device."
fi
echo "   The master password is created in the wizard, not here."
echo "   Reboot is not required to continue. On AMD GPU hardware a reboot"
echo "   applies kernel parameters — recommended, not a gate."
echo "======================================================="
log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_info "ACTION REQUIRED: Set BIOS 'Restore on AC Power Loss' → 'Power On'"
log_info "This ensures HomeBrain auto-starts after a power outage."
log_info "Location: BIOS → Power Management → AC Power Recovery"
log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
