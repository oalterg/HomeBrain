#!/bin/bash
set -euo pipefail

# Load Common Library
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# --- Configuration ---
LOCK_FILE="/var/run/homebrain-backup.lock"
BACKUP_LOG_FILE="$LOG_DIR/backup.log"
STRATEGY="full"

# Parse Args
while [[ $# -gt 0 ]]; do
  case $1 in
    --strategy)
      STRATEGY="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

# Redirect output to log file if not running interactively
if [ -t 1 ]; then
    : # Running in terminal, allow stdout
else
    exec >> "$BACKUP_LOG_FILE" 2>&1
fi

load_env

# --- Validation ---
: "${BACKUP_RETENTION:?BACKUP_RETENTION not set}"
: "${NEXTCLOUD_DATA_DIR:?NEXTCLOUD_DATA_DIR not set}"

# --- Locking ---
exec 200>"$LOCK_FILE"
flock -n 200 || die "Backup is already running."

# --- Staging and Cleanup ---
# Determine staging location. Use backup drive to avoid filling OS disk, 
# but ensure we have a fallback or cleaner error if mount fails.
STAGING_BASE="$BACKUP_MOUNTDIR"

# TRAP to ensure cleanup and maintenance mode is turned off on exit/error
cleanup() {
    log_info "Cleaning up..."
    set_maintenance_mode "--off"
    # Attempt to restart services if we crashed mid-backup
    if ! is_stack_running; then
        # Ensure HA is up if we stopped it
        local ha_cid=$(get_ha_cid)
        if [[ -n "$ha_cid" ]]; then docker start "$ha_cid" || true; fi
    fi
    
    # Remove staging directory safely
    if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" ]]; then
        rm -rf "$STAGING_DIR"
        log_info "Staging directory removed."
    fi
    rm -f "$LOCK_FILE"  # Ensure lock release
    log_info "Backup cleanup complete."
}
trap cleanup EXIT INT TERM

# Strategies:
#   full      — everything: NC data + DB + config + apps, HA, vault, OpenClaw
#   data_only — NC data + HA config (no DB, no NC config)
#   system    — everything EXCEPT NC data (DB dumps, configs, vault, OpenClaw).
#               Small + fast; used by update.sh as the pre-update snapshot.
case "$STRATEGY" in
    full|data_only|system) ;;
    *) die "Unknown backup strategy: $STRATEGY" ;;
esac

# --- Main Logic ---
log_info "=== Starting Backup [Strategy: $STRATEGY]: $(date) ==="

# 1. Mount Check
if ! mountpoint -q "$BACKUP_MOUNTDIR"; then
    log_info "Attempting to mount $BACKUP_MOUNTDIR..."
    mount "$BACKUP_MOUNTDIR" || die "Failed to mount backup drive."
fi

# Ensure backup dir is writable
if [ ! -w "$BACKUP_MOUNTDIR" ]; then
    die "Backup mount point is read-only or inaccessible."
fi

# 2. Check Service Health (Required to identify volumes)
HA_CID=$(get_ha_cid)
NC_CID=$(get_nc_cid)
DB_CID=$(get_nc_db_cid)

if [[ -z "$HA_CID" ]]; then log_warn "Home Assistant container not found. Skipping HA backup."; fi

# 3. Disk Space Check
log_info "[1/6] Checking for sufficient disk space..."
# Check DB connectivity for size estimation
wait_for_healthy "db" 60 || die "Database is not healthy, cannot perform backup."
# ensures valid configuration before querying Docker for database ID
DB_CID=$(get_tunnel_profiles >/dev/null; docker compose $(get_compose_args) ps -q db)

if [[ "$STRATEGY" == "system" ]]; then
    ESTIMATED_DATA_KB=0  # system snapshots skip the NC data tree
else
    ESTIMATED_DATA_KB=$(du -sk "$NEXTCLOUD_DATA_DIR" | awk '{print $1}')
fi
# Dynamically estimate DB size
ESTIMATED_DB_KB=$(docker exec "$DB_CID" mariadb -u "$MYSQL_USER" -p"$MYSQL_PASSWORD" -e "SELECT ROUND(SUM(data_length + index_length) / 1024) AS size_kb FROM information_schema.tables WHERE table_schema='$MYSQL_DATABASE';" 2>/dev/null | tail -1 || echo "102400")
ESTIMATED_CONFIG_KB=51200  # Conservative for config (50MB)
ESTIMATED_UNCOMPRESSED_KB=$((ESTIMATED_DATA_KB + ESTIMATED_DB_KB + ESTIMATED_CONFIG_KB))
# Peak: ~2.0x for staging + archive (assuming no compression; adjust multiplier if needed)
ESTIMATED_PEAK_KB=$((ESTIMATED_UNCOMPRESSED_KB * 2))
AVAILABLE_KB=$(df --output=avail "$BACKUP_MOUNTDIR" | tail -n1)

log_info "[INFO] Estimated uncompressed: $((ESTIMATED_UNCOMPRESSED_KB / 1024)) MB, Peak: $((ESTIMATED_PEAK_KB / 1024)) MB, Available: $((AVAILABLE_KB / 1024)) MB"

# Emergency Cleanup Loop
# Continuously delete oldest backups if space is insufficient, even beyond retention policy.
while [ "$AVAILABLE_KB" -lt "$ESTIMATED_PEAK_KB" ]; do
    log_warn "Insufficient space (Avail: $((AVAILABLE_KB/1024)) MB, Need: $((ESTIMATED_PEAK_KB/1024)) MB). searching for old backups to purge..."
    
    # Find oldest backup, sort by timestamp asc (oldest on top)
    OLDEST_BACKUP=$(find "$BACKUP_MOUNTDIR" -maxdepth 1 -type f \( -name "homebrain_backup*.tar.gz*" -o -name "nextcloud_backup*.tar.gz*" \) -printf "%T@ %p\n" | sort -n | head -n1 | awk '{print $2}')
    
    if [[ -z "$OLDEST_BACKUP" ]]; then
        die "CRITICAL: No old backups remain to delete, and space is still insufficient. Aborting."
    fi
    
    log_info "Emergency Prune: Deleting $OLDEST_BACKUP to free space."
    rm -f "$OLDEST_BACKUP"
    sync # Ensure free space is updated in kernel
    
    # Refresh available space
    AVAILABLE_KB=$(df --output=avail "$BACKUP_MOUNTDIR" | tail -n1)
done

# 4. Prepare Staging
DATE="$(date +'%Y-%m-%d_%H-%M-%S')"
SUFFIX=""
if [[ "$STRATEGY" == "data_only" ]]; then SUFFIX="_data_only"; fi
if [[ "$STRATEGY" == "system" ]]; then SUFFIX="_system"; fi
STAGING_DIR=$(mktemp -d -p "$STAGING_BASE" staging_XXXXXX)
ARCHIVE_PATH="$BACKUP_MOUNTDIR/homebrain_backup${SUFFIX}_${DATE}.tar.gz"

# Encryption: on unless explicitly disabled. The passphrase is the master
# password AT BACKUP TIME — each archive is self-contained (gpg stores the
# s2k salt in the header), so restoring after a master-password rotation just
# needs the password that was current when the archive was made.
ENCRYPT=false
if [[ "${BACKUP_ENCRYPT:-true}" != "false" ]] && [[ -n "${MASTER_PASSWORD:-}" ]]; then
    ENCRYPT=true
    ARCHIVE_PATH="${ARCHIVE_PATH}.gpg"
elif [[ "${BACKUP_ENCRYPT:-true}" != "false" ]]; then
    log_warn "MASTER_PASSWORD not set — backup will NOT be encrypted."
fi

mkdir -p "$STAGING_DIR/nc_data" "$STAGING_DIR/nc_apps" "$STAGING_DIR/nc_db" "$STAGING_DIR/nc_config" "$STAGING_DIR/ha_config"

# ── Portable instance secrets ───────────────────────────────────────────────
# These .env entries are NOT per-install identity (MASTER_PASSWORD,
# VAULT_ADMIN_TOKEN/NONCE, NEWT_* etc. stay with the box). They are
# derivation keys whose value the user-facing data is bound to:
#
#   HOMEBRAIN_EMAIL_KEY    Fernet key used to encrypt every multi-account
#                          token at rest (email, HA, NC). Restoring the
#                          *_accounts.json files without this key onto a
#                          fresh instance would leave the user with
#                          undecryptable garbage — they'd have to re-enter
#                          every IMAP password and re-issue every HA LLAT.
#                          The key itself derives from MASTER_PASSWORD via
#                          PBKDF2, but since each install has a different
#                          master pw, the cross-install Fernet key MUST be
#                          carried with the encrypted blobs.
#
#   HOMEBRAIN_SELF_NONCE   Per-install nonce for the self-MCP bearer token
#                          derivation. Restoring it keeps the bearer stable
#                          across the migration (no functional impact
#                          either way — the dashboard re-derives on start
#                          — but quieter logs).
#
# Format: shell-sourceable, NOT a complete .env (we strip everything else).
INSTANCE_SECRETS_FILE="$STAGING_DIR/instance_secrets.env"
{
    [[ -n "${HOMEBRAIN_EMAIL_KEY:-}" ]] && echo "HOMEBRAIN_EMAIL_KEY=${HOMEBRAIN_EMAIL_KEY}"
    [[ -n "${HOMEBRAIN_SELF_NONCE:-}" ]] && echo "HOMEBRAIN_SELF_NONCE=${HOMEBRAIN_SELF_NONCE}"
} > "$INSTANCE_SECRETS_FILE"
if [[ -s "$INSTANCE_SECRETS_FILE" ]]; then
    chmod 600 "$INSTANCE_SECRETS_FILE"
    log_info "Portable instance secrets captured for cross-instance restore."
else
    rm -f "$INSTANCE_SECRETS_FILE"
fi

# 5. Stop Services / Enable Maintenance Mode
log_info "Preparing services..."

# Stop OpenClaw daemon for consistent snapshot
if [[ "${HAS_GPU:-false}" == "true" ]] && command -v openclaw &>/dev/null; then
    log_info "Stopping OpenClaw daemon for consistent backup..."
    timeout 10 sudo -u "${HOMEBRAIN_USER}" openclaw daemon stop 2>/dev/null \
        || log_warn "OpenClaw daemon stop timed out — backup may be inconsistent"
fi

set_maintenance_mode "--on"

# STOP Home Assistant to ensure SQLite DB consistency
if [[ -n "$HA_CID" ]]; then
    log_info "Stopping Home Assistant..."
    docker stop "$HA_CID"
fi

# 6. Database Dump (full + system)
if [[ "$STRATEGY" != "data_only" && -n "$DB_CID" ]]; then
    log_info "Dumping Nextcloud Database..."

    # Health check first
    docker run --rm \
        --network container:"$DB_CID" \
        -e MYSQL_PWD="$MYSQL_PASSWORD" \
        mysql:8 \
        mysqladmin -h 127.0.0.1 -u "$MYSQL_USER" ping >/dev/null || die "Database is not responding."

# Then dump (clean output)
    docker run --rm \
        --network container:"$DB_CID" \
        -e MYSQL_PWD="$MYSQL_PASSWORD" \
        mysql:8 \
        mysqldump --column-statistics=0 -h 127.0.0.1 -u "$MYSQL_USER" "$MYSQL_DATABASE" \
        > "$STAGING_DIR/nc_db/nextcloud.sql" || die "Database dump failed."

    # Verify dump is not empty
    if [ ! -s "$STAGING_DIR/nc_db/nextcloud.sql" ]; then
        die "Database dump created but file is empty. Backup aborted."
    fi
fi

# 6.5 Nextcloud Apps (full + system - To backup installed apps like Passwords)
if [[ "$STRATEGY" != "data_only" && -n "$NC_CID" ]]; then
    log_info "Syncing Nextcloud Custom User Apps..."

    # 1. Identify the volume mounted at /var/www/html
    NC_VOL=$(docker inspect "$NC_CID" --format '{{ range .Mounts }}{{ if eq .Destination "/var/www/html" }}{{ .Name }}{{ end }}{{ end }}')
    
    # 2. Backup only /custom_apps
    # We mount the whole html volume to /volume, then copy /volume/custom_apps
    docker run --rm -v "${NC_VOL}:/volume:ro" -v "$STAGING_DIR/nc_apps":/backup alpine \
        sh -c "if [ -d /volume/custom_apps ]; then cp -a /volume/custom_apps/. /backup/; fi" || die "NC Apps backup failed."
fi

# 7. Nextcloud Data (Rsync host path — skipped for system snapshots)
if [[ "$STRATEGY" != "system" ]]; then
    log_info "Syncing Nextcloud Data..."
    rsync -a --delete "$NEXTCLOUD_DATA_DIR"/ "$STAGING_DIR/nc_data/" || die "NC Data Sync failed."
else
    rmdir "$STAGING_DIR/nc_data" 2>/dev/null || true
fi

# 8. Nextcloud Config (Helper Container - full + system)
if [[ "$STRATEGY" != "data_only" && -n "$NC_CID" ]]; then
    log_info "Syncing Nextcloud Config..."
    NC_VOL=$(docker inspect "$NC_CID" --format '{{ range .Mounts }}{{ if eq .Destination "/var/www/html" }}{{ .Name }}{{ end }}{{ end }}')
    docker run --rm -v "${NC_VOL}:/volume:ro" -v "$STAGING_DIR/nc_config":/backup alpine \
        sh -c "cp -a /volume/config/. /backup/" || die "NC Config backup failed."
fi

# 9. Home Assistant Config (Helper Container - All Strategies)
if [[ -n "$HA_CID" ]]; then
    log_info "Syncing Home Assistant Config..."
    # We use --volumes-from because HA uses a named volume, not a bind mount.
    # Note: HA_CID is stopped, but we can still mount its volumes using the ID.
    docker run --rm --volumes-from "$HA_CID" \
        -v "$STAGING_DIR/ha_config":/backup \
        alpine sh -c "cp -a /config/. /backup/" || die "HA Config backup failed."
fi

# ── OpenClaw Config & Workspace ─────────────────────────────────────────────
if [[ "${HAS_GPU:-false}" == "true" ]]; then
    OPENCLAW_DIR="${HOMEBRAIN_HOME}/.openclaw"

    # Always back up config file
    if [[ -f "${OPENCLAW_DIR}/openclaw.json" ]]; then
        mkdir -p "${STAGING_DIR}/openclaw_config"
        cp "${OPENCLAW_DIR}/openclaw.json" "${STAGING_DIR}/openclaw_config/"
        log_info "OpenClaw config backed up."
    else
        log_warn "OpenClaw config not found at ${OPENCLAW_DIR}/openclaw.json — skipping."
    fi

    # Integration credentials (HA accounts, NC accounts, email accounts,
    # Self bearer token, vault session, pending consent state). Mode 0600,
    # owned by homebrain — preserve perms on restore. Legacy *.token files
    # (ha.token, nextcloud.token, homebrain.token) are included too so a
    # box that has not yet been migrated still backs up cleanly; the
    # dashboard's first integration_status() call after restore folds them
    # into the new accounts store and deletes them.
    if compgen -G "${OPENCLAW_DIR}/*.token" > /dev/null \
        || compgen -G "${OPENCLAW_DIR}/*_accounts.json" > /dev/null \
        || [[ -f "${OPENCLAW_DIR}/pending_actions.json" ]] \
        || [[ -f "${OPENCLAW_DIR}/vault.session" ]]; then
        mkdir -p "${STAGING_DIR}/openclaw_integrations"
        for f in ha.token nextcloud.token homebrain.token vault.session \
                 ha_accounts.json nc_accounts.json email_accounts.json \
                 pending_actions.json; do
            [[ -f "${OPENCLAW_DIR}/${f}" ]] && cp -a "${OPENCLAW_DIR}/${f}" "${STAGING_DIR}/openclaw_integrations/"
        done
        log_info "OpenClaw integration credentials backed up."
    fi

    # Per-integration audit logs — last 30 days only, capped to keep the
    # archive bounded. The full log lives at /var/log/homebrain/.
    if compgen -G "/var/log/homebrain/mcp-*-audit.log" > /dev/null; then
        mkdir -p "${STAGING_DIR}/mcp_audit"
        for src in /var/log/homebrain/mcp-*-audit.log; do
            # Keep only entries newer than 30 days; if jq isn't available
            # fall back to a tail of the last 5000 lines.
            base=$(basename "$src")
            if command -v jq >/dev/null 2>&1; then
                cutoff=$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
                         || date -u -v-30d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
                if [[ -n "$cutoff" ]]; then
                    jq -c --arg c "$cutoff" 'select(.ts >= $c)' "$src" \
                        > "${STAGING_DIR}/mcp_audit/${base}" 2>/dev/null \
                        || tail -n 5000 "$src" > "${STAGING_DIR}/mcp_audit/${base}"
                else
                    tail -n 5000 "$src" > "${STAGING_DIR}/mcp_audit/${base}"
                fi
            else
                tail -n 5000 "$src" > "${STAGING_DIR}/mcp_audit/${base}"
            fi
        done
        log_info "MCP audit logs backed up."
    fi

    # Workspace backup (opt-out: default true)
    if [[ "${BACKUP_OPENCLAW_WORKSPACE:-true}" == "true" ]]; then
        if [[ -d "${OPENCLAW_DIR}/workspace" ]]; then
            WS_SIZE=$(du -sm "${OPENCLAW_DIR}/workspace" 2>/dev/null | cut -f1)
            WS_WARN_MB="${BACKUP_OPENCLAW_SIZE_WARN_MB:-500}"
            if [[ -n "${WS_SIZE}" && "${WS_SIZE}" -gt "${WS_WARN_MB}" ]]; then
                log_warn "OpenClaw workspace is ${WS_SIZE} MB (threshold: ${WS_WARN_MB} MB). Consider enabling BACKUP_OPENCLAW_EXCLUDE_CACHES=true."
            fi
            RSYNC_EXCLUDES=()
            if [[ "${BACKUP_OPENCLAW_EXCLUDE_CACHES:-false}" == "true" ]]; then
                RSYNC_EXCLUDES+=(--exclude="cache/" --exclude="*.tmp")
            fi
            mkdir -p "${STAGING_DIR}/openclaw_workspace"
            rsync -a --quiet "${RSYNC_EXCLUDES[@]}" \
                "${OPENCLAW_DIR}/workspace/" "${STAGING_DIR}/openclaw_workspace/"
            log_info "OpenClaw workspace backed up (${WS_SIZE:-unknown} MB)."
        else
            log_warn "OpenClaw workspace directory not found — skipping."
        fi
    fi
fi

# ── Vault (Vaultwarden) ─────────────────────────────────────────────────────
# Backup the vault DB + data dir. The rsa_key.* files inside the data dir are
# critical — losing them invalidates every active Bitwarden client session.
VAULT_CID=$(get_vault_cid 2>/dev/null || true)
if [[ "${VAULT_ENABLED:-true}" == "true" ]] && [[ -n "$VAULT_CID" ]]; then
    log_info "Stopping Vaultwarden for consistent backup..."
    docker stop "$VAULT_CID" >/dev/null 2>&1 || log_warn "Failed to stop vaultwarden (continuing)."

    if [[ -n "$DB_CID" ]] && [[ -n "${VAULT_DB_NAME:-}" ]] && [[ -n "${VAULT_DB_USER:-}" ]] && [[ -n "${VAULT_DB_PASSWORD:-}" ]]; then
        mkdir -p "$STAGING_DIR/vault_db"
        docker run --rm \
            --network container:"$DB_CID" \
            -e MYSQL_PWD="$VAULT_DB_PASSWORD" \
            mysql:8 \
            mysqldump --column-statistics=0 -h 127.0.0.1 -u "$VAULT_DB_USER" "$VAULT_DB_NAME" \
            > "$STAGING_DIR/vault_db/vaultwarden.sql" 2>/dev/null \
            || log_warn "Vault DB dump failed (non-fatal)."
        if [[ ! -s "$STAGING_DIR/vault_db/vaultwarden.sql" ]]; then
            log_warn "Vault DB dump empty — vault may be uninitialised."
        fi
    fi

    VAULT_DATA="${VAULT_DATA_DIR:-${HOMEBRAIN_HOME}/vault-data}"
    if [[ -d "$VAULT_DATA" ]]; then
        mkdir -p "$STAGING_DIR/vault_data"
        rsync -a "$VAULT_DATA"/ "$STAGING_DIR/vault_data/" || log_warn "Vault data rsync failed (non-fatal)."
        if [[ ! -f "$STAGING_DIR/vault_data/rsa_key.pem" ]] && [[ ! -f "$STAGING_DIR/vault_data/rsa_key.pkcs8.der" ]]; then
            log_warn "Vault rsa_key not present in archive — sessions may need re-login after restore."
        fi
    fi

    log_info "Restarting Vaultwarden..."
    docker start "$VAULT_CID" >/dev/null 2>&1 || log_warn "Failed to restart vaultwarden — start manually if needed."
fi

# 10. Restart Services
log_info "Resuming services..."
if [[ -n "$HA_CID" ]]; then docker start "$HA_CID"; fi
set_maintenance_mode "--off"

# Restart OpenClaw daemon
if [[ "${HAS_GPU:-false}" == "true" ]] && command -v openclaw &>/dev/null; then
    log_info "Restarting OpenClaw daemon..."
    sudo -u "${HOMEBRAIN_USER}" openclaw daemon start 2>/dev/null \
        || log_warn "OpenClaw daemon restart failed after backup — restart manually if needed"
fi

# 11. Compress (+ encrypt)
if [[ "$ENCRYPT" == "true" ]]; then
    log_info "Compressing + encrypting archive (AES256, passphrase = master password)..."
    tar -C "$STAGING_DIR" -cz . | gpg --batch --yes --symmetric \
        --cipher-algo AES256 --s2k-mode 3 --s2k-digest-algo SHA512 \
        --s2k-count 65011712 --compress-algo none \
        --passphrase-fd 3 -o "$ARCHIVE_PATH" 3<<<"$MASTER_PASSWORD" \
        || { rm -f "$ARCHIVE_PATH"; die "Compression/encryption failed."; }
else
    log_info "Compressing archive (unencrypted — BACKUP_ENCRYPT=false)..."
    tar -C "$STAGING_DIR" -czf "$ARCHIVE_PATH" . || die "Compression failed."
fi
sync

# 12. Verify — read the whole archive back through the full decrypt/decompress
# pipeline. Catches truncated writes and bad sectors while the staging data
# still exists. Skippable for huge archives via BACKUP_VERIFY=false.
if [[ "${BACKUP_VERIFY:-true}" != "false" ]]; then
    log_info "Verifying archive integrity..."
    if [[ "$ENCRYPT" == "true" ]]; then
        gpg --batch --quiet --decrypt --passphrase-fd 3 "$ARCHIVE_PATH" 3<<<"$MASTER_PASSWORD" \
            | tar -tz > /dev/null \
            || { rm -f "$ARCHIVE_PATH"; die "Archive verification FAILED — bad archive deleted."; }
    else
        tar -tzf "$ARCHIVE_PATH" > /dev/null \
            || { rm -f "$ARCHIVE_PATH"; die "Archive verification FAILED — bad archive deleted."; }
    fi
    log_info "Archive verified."
fi

# 13. Retention
log_info "Applying retention (Keep: $BACKUP_RETENTION)..."

# List all naming schemas (plain + .gpg), sort by time (newest first), skip
# first N, delete rest. Pre-update system snapshots are counted separately
# (keep 2) so update churn can never push real backups out of retention.
find "$BACKUP_MOUNTDIR" -maxdepth 1 -type f \( -name "homebrain_backup*.tar.gz*" -o -name "nextcloud_backup*.tar.gz*" \) ! -name "homebrain_backup_system_*" -printf "%T@ %p\n" | \
    sort -rn | \
    awk -v keep="$BACKUP_RETENTION" 'NR > keep {print $2}' | \
    xargs -r rm --

find "$BACKUP_MOUNTDIR" -maxdepth 1 -type f -name "homebrain_backup_system_*.tar.gz*" -printf "%T@ %p\n" | \
    sort -rn | \
    awk 'NR > 2 {print $2}' | \
    xargs -r rm --

log_info "=== Backup Complete: $ARCHIVE_PATH ==="
# Lock file removed by trap
