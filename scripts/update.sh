#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# --- Configuration ---
REPO_OWNER="oalterg"
REPO_NAME="HomeBrain"
INSTALL_DIR="/opt/homebrain"
LOG_FILE="/var/log/homebrain/manager_update.log"
ENV_FILE="$INSTALL_DIR/.env"

if [ -t 1 ]; then :; else exec >> "$LOG_FILE" 2>&1; fi

log_info "========================================"
log_info "Update Started: $(date)"
log_info "Channel: ${1:-stable} | Target: ${2:-main}"

# Arguments: $1 = Channel (stable/beta), $2 = Target Ref
CHANNEL="${1:-stable}"
TARGET_REF="${2:-main}"

# --- Downgrade guard ------------------------------------------------------
# Gathers the installed vs. target version signals and aborts the update if it
# would move the stack backwards (see detect_downgrade in common.sh for why
# that is unrecoverable). Call with the path to the TARGET docker-compose.yml
# so we can compare the Nextcloud image tag; an empty/missing path falls back
# to the release-version signal alone. Honours ALLOW_DOWNGRADE=1 as a
# deliberate, backup-in-hand override.
abort_if_downgrade() {
    local target_compose="${1:-}"

    local inst_channel="" inst_ref=""
    if [[ -f "$INSTALL_DIR/version.json" ]] && command -v jq >/dev/null 2>&1; then
        inst_channel=$(jq -r '.channel // empty' "$INSTALL_DIR/version.json" 2>/dev/null || echo "")
        inst_ref=$(jq -r '.ref // empty' "$INSTALL_DIR/version.json" 2>/dev/null || echo "")
    fi

    local inst_nc="" tgt_nc=""
    [[ -f "$INSTALL_DIR/docker-compose.yml" ]] && inst_nc=$(parse_nc_tag "$INSTALL_DIR/docker-compose.yml")
    [[ -n "$target_compose" && -f "$target_compose" ]] && tgt_nc=$(parse_nc_tag "$target_compose")

    local reason
    if reason=$(detect_downgrade "$inst_channel" "$inst_ref" "$CHANNEL" "$TARGET_REF" "$inst_nc" "$tgt_nc"); then
        if [[ "${ALLOW_DOWNGRADE:-0}" == "1" ]]; then
            log_warn "DOWNGRADE OVERRIDE (ALLOW_DOWNGRADE=1): $reason"
            log_warn "Proceeding anyway — Nextcloud and/or the dashboard may break. Hope you have a backup."
            return 0
        fi
        log_error "Refusing downgrade: $reason"
        log_error "HomeBrain updates are one-way: Nextcloud will not start on an older image once"
        log_error "its data has migrated, and the dashboard breaks on a mixed code tree."
        log_error "To go back, restore a pre-upgrade backup instead:"
        log_error "    sudo bash $INSTALL_DIR/scripts/restore.sh"
        log_error "If you understand the risk and have a backup, re-run with ALLOW_DOWNGRADE=1."
        exit 1
    fi
}

# 0. Self-Update Check
# Ensure we start clean even if a previous run aborted without cleanup
SELF_TMP_DIR="/tmp/homebrain_self_update"
rm -rf "$SELF_TMP_DIR"
mkdir -p "$SELF_TMP_DIR"
trap 'rm -rf "$SELF_TMP_DIR"' EXIT

log_info "Checking for update script changes..."

if [ "$CHANNEL" == "stable" ]; then
    BASE_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$TARGET_REF/scripts"
else
    BASE_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/scripts"
fi

# Hardened curl: timeout, retries, fail on error
curl -L -f -s --max-time 30 --retry 3 --retry-delay 5 "$BASE_URL/update.sh" -o "$SELF_TMP_DIR/update.sh" || { log_error "Failed to fetch new update script"; exit 1; }
curl -L -f -s --max-time 30 --retry 3 --retry-delay 5 "$BASE_URL/common.sh" -o "$SELF_TMP_DIR/common.sh" || { log_error "Failed to fetch new common script"; exit 1; }

chmod +x "$SELF_TMP_DIR/update.sh"

# Downgrade guard (Layer 1) — MUST run before the self-update re-exec below.
# A stable->old update self-updates to the target's update.sh and exec's it;
# older scripts have no guard, so checking here is the only place that protects
# the re-exec. Fetch the target docker-compose.yml (one level up from BASE_URL)
# for the authoritative Nextcloud tag; if that fetch fails we still catch the
# common beta->stable / older-tag cases from version.json alone.
RAW_ROOT="${BASE_URL%/scripts}"
TARGET_COMPOSE="$SELF_TMP_DIR/target-compose.yml"
curl -L -f -s --max-time 30 --retry 2 --retry-delay 3 "$RAW_ROOT/docker-compose.yml" -o "$TARGET_COMPOSE" \
    || { log_warn "Could not fetch target docker-compose.yml; checking release version only."; rm -f "$TARGET_COMPOSE"; }
abort_if_downgrade "$TARGET_COMPOSE"

CURRENT_UPDATE="$SCRIPT_DIR/update.sh"
CURRENT_COMMON="$SCRIPT_DIR/common.sh"

if ! cmp -s "$CURRENT_UPDATE" "$SELF_TMP_DIR/update.sh" || ! cmp -s "$CURRENT_COMMON" "$SELF_TMP_DIR/common.sh" ; then
    log_info "Changes detected in update.sh or common.sh. Reloading with new versions..."
    exec "$SELF_TMP_DIR/update.sh" "$CHANNEL" "$TARGET_REF"
fi

log_info "Update script and common up-to-date. Proceeding..."

load_env

# 1. Prepare Environment
mkdir -p "/tmp" # Ensure tmp exists
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR" "$SELF_TMP_DIR"' EXIT  # Double-trap for safety

# 2. Download Artifact
if [ "$CHANNEL" == "stable" ]; then
    URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/tags/$TARGET_REF.tar.gz"
else
    URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/main.tar.gz"
fi

log_info "Downloading from $URL..."
curl -L -f -s --max-time 60 --retry 3 --retry-delay 5 "$URL" -o "$TEMP_DIR/update.tar.gz" || { log_error "Download failed"; exit 1; }

# 3. Extract
log_info "Extracting..."
mkdir -p "$TEMP_DIR/extract"
tar -xzf "$TEMP_DIR/update.tar.gz" --strip-components=1 -C "$TEMP_DIR/extract" || { log_error "Extraction failed"; exit 1; }

# Downgrade guard (Layer 2) — authoritative, network-free re-check now that the
# real target tree is on disk. Runs before the destructive rsync so a downgrade
# that slipped past the best-effort Layer 1 fetch still aborts before any data
# or code is touched.
abort_if_downgrade "$TEMP_DIR/extract/docker-compose.yml"

# 4. Atomic File Sync
log_info "Applying file updates, preserving configuration..."

# Capture pinned dep versions before sync so we can detect bumps afterward
old_llama_tag=""
old_openclaw_ver=""
old_vault_tag=""
if command -v jq >/dev/null 2>&1 && [[ -f "$INSTALL_DIR/config/versions.json" ]]; then
    old_llama_tag=$(jq -r '.llama_cpp.tag // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
    old_openclaw_ver=$(jq -r '.openclaw.version // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
    old_vault_tag=$(jq -r '.vaultwarden.tag // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
fi

# Back up the docker-compose.yml just in case
cp "$INSTALL_DIR/docker-compose.yml" "$TEMP_DIR/extract/docker-compose.yml.backup"

# rsync ensures we get new files, delete removed files, but exclude our preserved configs from being overwritten if they were missing in source
rsync -a --delete \
--exclude='.env' \
--exclude='.setup_complete' \
--exclude='docker-compose.override.yml' \
--exclude='.git' \
--exclude='version.json' \
--exclude='venv' \
"$TEMP_DIR/extract/" "$INSTALL_DIR/" || { log_error "Rsync failed"; exit 1; }

# 4b. Pre-update snapshot — DB dumps + configs + vault + OpenClaw (everything
# except the Nextcloud data tree, which updates don't touch), so "restore a
# pre-upgrade backup" is always possible instead of a hope. Placed AFTER the
# rsync so the freshly-synced backup.sh (which understands --strategy system)
# runs, and BEFORE any docker image pull/up or dependency bump — the steps
# that actually migrate data forward. Non-fatal: a missing backup drive must
# not make updates impossible, but we warn loudly. SKIP_PREUPDATE_BACKUP=1
# skips it.
if [[ -f "$INSTALL_DIR/.setup_complete" && "${SKIP_PREUPDATE_BACKUP:-0}" != "1" ]]; then
    if mountpoint -q /mnt/backup 2>/dev/null; then
        log_info "Taking pre-update system snapshot..."
        if bash "$INSTALL_DIR/scripts/backup.sh" --strategy system; then
            log_info "Pre-update snapshot complete."
        else
            log_warn "PRE-UPDATE SNAPSHOT FAILED — continuing, but there is no fresh restore point for this update."
        fi
    else
        log_warn "No backup drive mounted — skipping pre-update snapshot."
    fi
fi

# Update pinned deps when versions.json changed in this release
if command -v jq >/dev/null 2>&1 && [[ -f "$INSTALL_DIR/config/versions.json" ]]; then
    new_llama_tag=$(jq -r '.llama_cpp.tag // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
    new_openclaw_ver=$(jq -r '.openclaw.version // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
    new_vault_tag=$(jq -r '.vaultwarden.tag // empty' "$INSTALL_DIR/config/versions.json" 2>/dev/null || echo "")
    # Vault tag drives the vaultwarden image — write it into .env so the next
    # `docker compose pull/up` picks the new image. Compose pull below handles
    # the actual rebuild.
    if [[ -n "$new_vault_tag" ]] && [[ "$old_vault_tag" != "$new_vault_tag" ]]; then
        log_info "Vaultwarden: ${old_vault_tag:-unset} → ${new_vault_tag}. Updating .env pin."
        if grep -q "^VAULTWARDEN_TAG=" "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^VAULTWARDEN_TAG=.*|VAULTWARDEN_TAG='${new_vault_tag}'|" "$ENV_FILE"
        else
            echo "VAULTWARDEN_TAG='${new_vault_tag}'" >> "$ENV_FILE"
        fi
    fi
    # Resolve from INSTALL_DIR, not SCRIPT_DIR: after the self-reload re-exec'd
    # the new update.sh from /tmp/homebrain_self_update, $SCRIPT_DIR points there
    # — and only update.sh + common.sh are downloaded into that dir. A
    # $SCRIPT_DIR-relative path would miss update-deps.sh entirely, so this whole
    # block (llama/openclaw version bumps AND the plugin/config drift catch-all)
    # would silently skip on every update that also changes update.sh. The
    # rsync above already put the current update-deps.sh in place under INSTALL_DIR.
    UPDATE_DEPS_SCRIPT="$INSTALL_DIR/scripts/update-deps.sh"
    if [[ -f "$UPDATE_DEPS_SCRIPT" ]] && [[ "${HAS_GPU:-false}" == "true" ]]; then
        if [[ -n "$new_llama_tag" && "$old_llama_tag" != "$new_llama_tag" ]]; then
            log_info "llama.cpp: ${old_llama_tag} → ${new_llama_tag}. Updating binary..."
            bash "$UPDATE_DEPS_SCRIPT" llama_cpp || log_warn "llama.cpp update failed — check logs."
            systemctl restart llama-server 2>/dev/null || true
        fi
        if [[ -n "$new_openclaw_ver" && "$old_openclaw_ver" != "$new_openclaw_ver" ]]; then
            log_info "OpenClaw: ${old_openclaw_ver} → ${new_openclaw_ver}. Updating..."
            bash "$UPDATE_DEPS_SCRIPT" openclaw || log_warn "OpenClaw update failed — check logs."
        else
            # Plugin + config drift catch-all. The openclaw npm package didn't
            # change, but two things still might have and are otherwise ONLY
            # applied by setup_openclaw (which the version-bump branch above runs
            # and this one does not):
            #   1. The bundled HomeBrain plugins under config/openclaw-plugins/
            #      (e.g. the WhatsApp QR login route). Without re-installing them
            #      here, a new/changed plugin would rsync into the install dir but
            #      never register with the gateway.
            #   2. The patch_openclaw_config jq pipeline in utilities.sh (new
            #      schema keys, refreshed allowedOrigins, etc.).
            # Delegate to utilities.sh's `refresh_openclaw` subcommand, run as a
            # SUBPROCESS — never `source` it. Sourcing utilities.sh executes
            # common.sh's top-level side effects under this script's `set -e`, and
            # resets SCRIPT_DIR to this script's $0 (the /tmp self-update dir after
            # the re-exec), so load_versions' `die` fires on the wrong path and
            # exits THIS script straight past any `|| true`. A subprocess keeps
            # utilities.sh's SCRIPT_DIR / set -e / die fully isolated — we only
            # read its return code. The subcommand snapshots openclaw.json,
            # (re)installs the bundled plugins + re-patches config, and restarts
            # the gateway only when the file actually changed.
            UTILS_FILE="$INSTALL_DIR/scripts/utilities.sh"
            if command -v openclaw >/dev/null 2>&1 && [[ -f "$UTILS_FILE" ]]; then
                log_info "Refreshing openclaw plugins + config against current install..."
                bash "$UTILS_FILE" refresh_openclaw \
                    || log_warn "openclaw plugin/config refresh failed — check logs."
            else
                log_warn "openclaw config refresh skipped: openclaw=$(command -v openclaw||echo missing) utils=$UTILS_FILE present=$([[ -f $UTILS_FILE ]]&&echo y||echo n)"
            fi
        fi
    fi
fi

# 5. Dependency Management
log_info "Updating Python dependencies..."
install_python_venv_deps

# 6a. Service Synchronization (Golden Master)
# This generalized block handles ALL service file updates (Gunicorn, Venv, or future changes).
# It ensures the active systemd units match the repository versions exactly.
UNITS_CHANGED=false
for UNIT in homebrain-manager.service homebrain-health.service homebrain-health.timer; do
    INSTALLED_SVC="/etc/systemd/system/$UNIT"
    REPO_SVC="$INSTALL_DIR/config/$UNIT"

    if [ -f "$REPO_SVC" ]; then
        # silent compare: returns 1 if different
        if ! cmp -s "$REPO_SVC" "$INSTALLED_SVC"; then
            log_info "$UNIT drift detected. Synchronizing with repository version..."

            # Backup and atomic replace
            [ -f "$INSTALLED_SVC" ] && cp "$INSTALLED_SVC" "${INSTALLED_SVC}.bak_$(date +%s)"
            cp "$REPO_SVC" "$INSTALLED_SVC"
            chmod 644 "$INSTALLED_SVC"
            UNITS_CHANGED=true
        fi
    else
        log_warn "Repository service file missing ($REPO_SVC). Skipping synchronization."
    fi
done
if [ "$UNITS_CHANGED" = true ]; then
    systemctl daemon-reload
    log_info "Service files synchronized."
fi
# The health timer ships disabled on boxes provisioned before it existed —
# enable it (idempotent). smartmontools is a provision-time dep; install it
# here once so pre-existing boxes get SMART monitoring too (best-effort).
systemctl enable --now homebrain-health.timer 2>/dev/null || true
command -v smartctl >/dev/null 2>&1 || apt-get install -y -qq smartmontools 2>/dev/null \
    || log_warn "smartmontools install failed — SMART monitoring disabled until next update."

# OS security patches apply themselves nightly (Debian defaults: security
# origin only, no automatic reboots — kernel updates wait for the next manual
# reboot). Idempotent, best-effort.
command -v unattended-upgrade >/dev/null 2>&1 || apt-get install -y -qq unattended-upgrades 2>/dev/null \
    || log_warn "unattended-upgrades install failed — OS security patches stay manual."
if command -v unattended-upgrade >/dev/null 2>&1; then
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
fi

# Remove the stale OpenClaw firewall allow older provisions added — the
# gateway binds loopback only and is reached via the manager's proxy, so the
# rule just advertised a closed port. Idempotent, no-op without ufw.
command -v ufw >/dev/null 2>&1 && ufw delete allow 18789/tcp >/dev/null 2>&1 || true

# Migrate backup scheduling cron -> persistent systemd timer. Cron silently
# skips runs the box sleeps through; the timer catches up on next boot. Only
# when a cron entry exists (i.e. the user actually configured backups) —
# never invent a schedule.
if [[ -f /etc/cron.d/homebrain-backup || -f /etc/cron.d/nextcloud-backup ]]; then
    log_info "Migrating backup schedule from cron to a persistent systemd timer..."
    bash "$INSTALL_DIR/scripts/utilities.sh" backup_timer \
        || log_warn "Backup timer migration failed — cron entry left in place."
fi


# 6b. Layout Migration (idempotent — _detect_migration_work probes first;
# no-op on already-consolidated targets, so this is safe to run every update).
# Runs AFTER rsync (so the latest migrate_to_consolidated_layout is in place)
# and BEFORE docker compose pull/up (so any data move + .env rewrite is
# reflected in the next mount).
#
# Resolve from INSTALL_DIR, not SCRIPT_DIR: when self-reload exec'd the new
# update.sh from /tmp/homebrain_self_update, $SCRIPT_DIR points there — and
# only update.sh + common.sh are downloaded into that dir, so utilities.sh
# would be missing and migration would silently skip.
INSTALLED_UTILS="$INSTALL_DIR/scripts/utilities.sh"
if [[ -x "$INSTALLED_UTILS" ]]; then
    log_info "Running directory consolidation migration..."
    if ! bash "$INSTALLED_UTILS" migrate; then
        log_warn "Migration reported issues; continuing — see /var/log/homebrain for details."
    fi
    # Migration may have rewritten NEXTCLOUD_DATA_DIR; reload so the
    # docker compose step below sees the canonical path.
    load_env
else
    log_warn "utilities.sh missing at $INSTALLED_UTILS — skipping migration."
fi

# 6. Docker Stack Update
log_info "Updating Docker Stack..."
cd "${INSTALL_DIR}" || die "Failed to cd to ${INSTALL_DIR}"
# Pull latest images defined in compose
docker compose --env-file "$ENV_FILE" $(get_compose_args) pull || { log_error "Docker pull failed"; exit 1; }
# Restart containers (recreates them if image changed or compose file changed)
profiles=$(get_tunnel_profiles)
docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} up -d --remove-orphans || { log_error "Docker up failed"; exit 1; }

# 6c. Nextcloud schema reconcile — a docker image bump lands new code in the
# html volume, but the DB migration still has to run. The image entrypoint's
# auto-upgrade is skipped when the container isn't recreated and can be left
# incomplete after a recovery, stranding NC on the "use the command line
# updater" page. reconcile_nextcloud is an idempotent no-op when nothing pends.
reconcile_nextcloud

# 7. Write Version File
cat > "$INSTALL_DIR/version.json" <<EOF
{
  "channel": "$CHANNEL",
  "ref": "$TARGET_REF",
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

log_info "Restarting Manager Service..."
systemctl restart homebrain-manager || { log_error "Service restart failed"; exit 1; }
