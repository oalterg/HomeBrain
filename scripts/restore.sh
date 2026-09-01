#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"
RESTORE_LOG_FILE="$LOG_DIR/restore.log"

# Log only if not running interactively
if [ -t 1 ]; then :; else exec >> "$RESTORE_LOG_FILE" 2>&1; fi

load_env

# --- Input Parsing ---
# Positional archive plus flags, in any order. --no-prompt used to be matched
# positionally as $2; keeping it a flag means --from-offsite can be added
# without callers caring about argument order.
BACKUP_FILE=""
NO_PROMPT=false
FROM_OFFSITE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-prompt)    NO_PROMPT=true ;;
        --from-offsite) FROM_OFFSITE=true ;;
        *)              [[ -z "$BACKUP_FILE" ]] && BACKUP_FILE="$1" ;;
    esac
    shift
done

# --- Prerequisites ---
# Shared with backup.sh so the two can't drift again: no-drive boxes
# (BACKUP_INTERNAL=true) have no mountpoint to check. See common.sh.
#
# Skipped when the archive is already readable. The guard exists so we can
# FIND an archive on the backup drive; insisting on the drive when we were
# handed a file that is right there refuses the exact restore a factory reset
# leaves you needing — .env is regenerated, the no-drive setting goes with it,
# and archives sitting in /var/backups/homebrain become unreachable.
#
# An off-site restore is the same argument taken one step further: it doesn't
# need an archive found, it needs somewhere to put the one it is fetching —
# and the box that needs it most is a replacement that has never had a drive.
if [[ "$FROM_OFFSITE" == "true" ]]; then
    ensure_staging_dir
elif [[ ! -f "$BACKUP_FILE" ]]; then
    ensure_backup_dir
fi

# --- Off-site fetch ---------------------------------------------------------
# The off-site copy existed but could only be written, never read: rclone
# pushed, nothing pulled, and restore.sh only ever looked at BACKUP_MOUNTDIR.
# The scenario off-site backups exist for — drive dead — required a shell.
# Pull the named archive down, then fall through to the normal path so there
# is exactly one restore implementation.
if [[ "$FROM_OFFSITE" == "true" ]]; then
    [[ -n "$BACKUP_FILE" ]] || die "--from-offsite requires an archive name."
    # Bare filename only: the caller may be passing something user-supplied.
    OFFSITE_NAME="$(basename "$BACKUP_FILE")"

    # Refuse a fetch that cannot fit. In internal-storage mode the target IS
    # the root disk, and an appliance whose root filesystem is full is an
    # appliance its owner cannot recover without a keyboard.
    NEED_KB=""
    REMOTE_BYTES="$(offsite_size "$OFFSITE_NAME" 2>/dev/null || true)"
    if [[ "$REMOTE_BYTES" =~ ^[0-9]+$ ]] && [[ "$REMOTE_BYTES" -gt 0 ]]; then
        NEED_KB=$(( REMOTE_BYTES / 1024 ))
        AVAIL_KB=$(df --output=avail "$BACKUP_MOUNTDIR" | tail -n1)
        if [[ "$AVAIL_KB" -lt "$NEED_KB" ]]; then
            die "Not enough space to fetch $OFFSITE_NAME: needs $((NEED_KB / 1024)) MB, $((AVAIL_KB / 1024)) MB free on $BACKUP_MOUNTDIR."
        fi
    else
        log_warn "Could not determine the remote size of $OFFSITE_NAME — fetching without a space check."
    fi

    log_info "Fetching $OFFSITE_NAME from the off-site remote..."
    offsite_fetch "$OFFSITE_NAME" "$BACKUP_MOUNTDIR" \
        || die "Could not fetch $OFFSITE_NAME from the off-site remote."
    BACKUP_FILE="$BACKUP_MOUNTDIR/$OFFSITE_NAME"
    [[ -f "$BACKUP_FILE" ]] \
        || die "Off-site fetch reported success but $OFFSITE_NAME is not present locally."
    log_info "Fetched $OFFSITE_NAME ($(du -h "$BACKUP_FILE" | cut -f1))."
fi

if [[ -z "$BACKUP_FILE" ]]; then
    # Auto-select latest (plain or encrypted)
    # -r matters: without it, xargs still runs `ls -t` once on empty input,
    # which lists the CWD and hands back an unrelated filename as the archive
    # to restore. The -f test below catches a directory, but a regular file in
    # the CWD would sail straight through into the restore.
    BACKUP_FILE="$(find "$BACKUP_MOUNTDIR" -maxdepth 1 \( -name '*backup*.tar.gz' -o -name '*backup*.tar.gz.gpg' \) -print0 | xargs -0 -r ls -t | head -n1)"
fi

if [[ -z "$BACKUP_FILE" || ! -f "$BACKUP_FILE" ]]; then
    die "Backup file not found or invalid selection: ${BACKUP_FILE:-None}"
fi

# Interactive confirmation
if [[ "$NO_PROMPT" != "true" ]]; then
    echo "⚠️ WARNING: RESTORE PROCESS INITIATED ⚠️"
    echo "Restoring: $BACKUP_FILE"
    echo "This will WIPE ALL DATA in: ${NEXTCLOUD_DATA_DIR:-${HOMEBRAIN_HOME}/nextcloud-data}"
    read -p "Type 'wipe' to confirm: " confirm
    if [[ "$confirm" != "wipe" ]]; then
        echo "Restore aborted by user."
        exit 0
    fi
fi

# --- Encryption Detection ---
# Encrypted archives (backup.sh with BACKUP_ENCRYPT, the default) end in
# .tar.gz.gpg. HBK1 archives wrap the GPG passphrase so either the master
# password or the recovery phrase opens them. Legacy files (no HBK1 header)
# still need the master password that was current when they were made.
# The dashboard may pass RESTORE_PASSPHRASE_FILE (a root-only temp file,
# shredded here after reading).
ENCRYPTED=false
[[ "$BACKUP_FILE" == *.gpg ]] && ENCRYPTED=true
SUPPLIED_PASS=""
if [[ -n "${RESTORE_PASSPHRASE_FILE:-}" && -f "${RESTORE_PASSPHRASE_FILE}" ]]; then
    SUPPLIED_PASS="$(cat "$RESTORE_PASSPHRASE_FILE")"
    rm -f "$RESTORE_PASSPHRASE_FILE"
fi
RESTORE_PASS="${SUPPLIED_PASS:-${MASTER_PASSWORD:-}}"

# --- Integrity Check ---
# For encrypted archives extraction itself is the integrity check (gpg MDC +
# gzip CRC), and it happens below — before any service is stopped — so a bad
# archive or wrong passphrase aborts with the system untouched.
if [[ "$ENCRYPTED" == "false" ]]; then
    log_info "Verifying backup integrity..."
    if ! gzip -t "$BACKUP_FILE"; then die "Corrupt backup file."; fi
fi

# --- Restore Logic ---
TMP_DIR=$(mktemp -d -p "${HOMEBRAIN_HOME}")
trap 'rm -rf "$TMP_DIR" ${DEK_DIR:+"$DEK_DIR"}; log_info "Cleanup done."' EXIT
if [ ! -d "$TMP_DIR" ]; then
    die "Failed to create temporary directory."
fi

log_info "Checking for sufficient disk space first"
if [[ "$ENCRYPTED" == "true" ]]; then
    # No gzip trailer to read through gpg; archives are mostly incompressible
    # media, so compressed ≈ uncompressed. 1.5x gives headroom.
    REQUIRED_SPACE=$(( $(du -sb "$BACKUP_FILE" | cut -f1) * 3 / 2 ))
else
    REQUIRED_SPACE=$(gzip -l "$BACKUP_FILE" | awk 'NR==2 {print int($2 * 1.1)}' || echo $(( $(du -sb "$BACKUP_FILE" | cut -f1) * 5 )))
fi
AVAILABLE_SPACE=$(df -B1 "$TMP_DIR" | tail -1 | awk '{print $4}')
if [ "$REQUIRED_SPACE" -gt "$AVAILABLE_SPACE" ]; then
    die "Insufficient space in $TMP_DIR for extraction (need ${REQUIRED_SPACE} bytes, have ${AVAILABLE_SPACE})."
fi

log_info "Extracting backup to temporary location $TMP_DIR..."
if [[ "$ENCRYPTED" == "true" ]]; then
    HB_PYTHON="$(backup_crypto_python)"
    # No helper on this box at all (a checkout predating HBK1) is the one case
    # where "assume legacy" is right. Anything else — an unreadable header, a
    # python without `cryptography`, a header from a newer build — must fail
    # here. Guessing legacy would hand the secret to gpg on an HBK1 body and
    # report a wrong passphrase for a perfectly good phrase.
    FMT="legacy"
    if [[ -f "$BACKUP_CRYPTO" ]]; then
        FMT="$("$HB_PYTHON" "$BACKUP_CRYPTO" inspect --archive "$BACKUP_FILE" --field format 2>/dev/null || true)"
    fi
    if [[ "$FMT" == "hbk1" ]]; then
        DEK_DIR=$(mktemp -d)
        chmod 700 "$DEK_DIR"
        DEK_FILE="$DEK_DIR/dek"
        opened=false
        try_secret() {
            local s="$1"
            [[ -n "$s" ]] || return 1
            printf '%s' "$s" > "$DEK_DIR/secret"
            chmod 600 "$DEK_DIR/secret"
            "$HB_PYTHON" "$BACKUP_CRYPTO" open --archive "$BACKUP_FILE" \
                --secret-file "$DEK_DIR/secret" --dek-file "$DEK_FILE"
        }
        if try_secret "$SUPPLIED_PASS"; then
            opened=true
        elif [[ -n "${MASTER_PASSWORD:-}" && "$MASTER_PASSWORD" != "$SUPPLIED_PASS" ]] \
                && try_secret "$MASTER_PASSWORD"; then
            opened=true
        fi
        if [[ "$opened" != true ]]; then
            die "Decryption failed — wrong passphrase or corrupt archive. Enter the master password from when this backup was made, or the recovery phrase."
        fi
        "$HB_PYTHON" "$BACKUP_CRYPTO" copy-body --archive "$BACKUP_FILE" \
            | gpg --batch --quiet --decrypt --passphrase-fd 3 3< "$DEK_FILE" \
            | tar -xz -C "$TMP_DIR" \
            || die "Decryption failed — wrong passphrase or corrupt archive. Enter the master password from when this backup was made, or the recovery phrase."
        rm -rf "$DEK_DIR"; DEK_DIR=""
    elif [[ "$FMT" == "legacy" ]]; then
        [[ -n "$RESTORE_PASS" ]] || die "Archive is encrypted but no passphrase is available (set MASTER_PASSWORD or provide one in the dashboard)."
        gpg --batch --quiet --decrypt --passphrase-fd 3 "$BACKUP_FILE" 3<<<"$RESTORE_PASS" \
            | tar -xz -C "$TMP_DIR" \
            || die "Decryption failed — wrong passphrase or corrupt archive. If this backup predates a master-password change, enter the password that was current when it was made."
    else
        die "Could not read this archive's header (backup_crypto reported '${FMT:-nothing}'). The file may be truncated, or written by a newer HomeBrain. Nothing was changed."
    fi
else
    tar -xzf "$BACKUP_FILE" -C "$TMP_DIR"
fi

# ── Portable instance-secret merge ──────────────────────────────────────────
# Pull HOMEBRAIN_EMAIL_KEY / HOMEBRAIN_SELF_NONCE from the archive (if
# present) into the destination .env BEFORE we start any containers or
# restore the openclaw_integrations directory. Without this step, the
# *_accounts.json files (multi-account email / HA / NC) are encrypted
# with the source instance's Fernet key and the destination would derive
# a different one from its own MASTER_PASSWORD — leaving the user with
# undecryptable account tokens. See backup.sh for the rationale.
if [[ -f "${TMP_DIR}/instance_secrets.env" ]]; then
    log_info "Merging portable instance secrets from backup..."
    merge_instance_secrets "${TMP_DIR}/instance_secrets.env"
fi

# --- Smart Detection ---
log_info "Analyzing backup structure..."

HAS_NC_DATA=false
HAS_NC_APPS=false
HAS_NC_DB=false
HAS_NC_CONFIG=false
HAS_HA_CONFIG=false
HAS_OPENCLAW_CONFIG=false
HAS_OPENCLAW_WORKSPACE=false
HAS_VAULT_DB=false
HAS_VAULT_DATA=false

# Check for legacy (root/data) or new (nc_data) folder structures
if [[ -d "$TMP_DIR/nc_data" ]] || [[ -d "$TMP_DIR/data" ]]; then HAS_NC_DATA=true; fi
if [[ -d "$TMP_DIR/nc_apps" ]]; then HAS_NC_APPS=true; fi
if [[ -d "$TMP_DIR/nc_db" ]] || [[ -f "$TMP_DIR/db/nextcloud.sql" ]]; then HAS_NC_DB=true; fi
if [[ -d "$TMP_DIR/nc_config" ]] || [[ -d "$TMP_DIR/config" ]]; then HAS_NC_CONFIG=true; fi
if [[ -d "$TMP_DIR/ha_config" ]]; then HAS_HA_CONFIG=true; fi
[[ -d "${TMP_DIR}/openclaw_config" ]] && HAS_OPENCLAW_CONFIG=true
[[ -d "${TMP_DIR}/openclaw_workspace" ]] && HAS_OPENCLAW_WORKSPACE=true
[[ -f "${TMP_DIR}/vault_db/vaultwarden.sql" ]] && HAS_VAULT_DB=true
[[ -d "${TMP_DIR}/vault_data" ]] && HAS_VAULT_DATA=true
HAS_ESCROW=false
HAS_ESCROW_WRAP=false
[[ -f "${TMP_DIR}/member_escrow.json" ]] && HAS_ESCROW=true
[[ -f "${TMP_DIR}/member_escrow.wrap" ]] && HAS_ESCROW_WRAP=true

log_info "Backup Contents: NC_DATA=$HAS_NC_DATA, NC_DB=$HAS_NC_DB, NC_CONFIG=$HAS_NC_CONFIG, HA=$HAS_HA_CONFIG"
log_info "OpenClaw config in archive: ${HAS_OPENCLAW_CONFIG} | workspace: ${HAS_OPENCLAW_WORKSPACE}"
log_info "Vault in archive: db=${HAS_VAULT_DB} data=${HAS_VAULT_DATA} escrow=${HAS_ESCROW}"

if [ "$HAS_NC_DATA" = false ] && [ "$HAS_HA_CONFIG" = false ]; then
    die "Invalid backup: No Data or HA config found."
fi

# A relocated data directory that is not a mount point means the files drive is
# absent. Restoring anyway writes the whole library onto the root disk at the
# mountpoint, fills it, and hides it under the drive on the next boot. Refuse
# here — before a single container is stopped, so the box keeps serving.
# :- covers unset AND empty — .env.template ships NEXTCLOUD_DATA_DIR= blank, and
# under set -u a bare expansion would abort the restore instead of guarding it.
NC_DATA_DEFAULT="${HOMEBRAIN_HOME}/nextcloud-data"
NC_DATA_EFFECTIVE="${NEXTCLOUD_DATA_DIR:-$NC_DATA_DEFAULT}"
if [ "$HAS_NC_DATA" = true ] \
    && [ "$NC_DATA_EFFECTIVE" != "$NC_DATA_DEFAULT" ] \
    && ! mountpoint -q "$NC_DATA_EFFECTIVE"; then
    die "Your files drive is not connected. $NC_DATA_EFFECTIVE should be a drive but nothing is mounted there. Reconnect it and run the restore again — nothing has been changed."
fi

# --- Stop Stack ---
log_info "Stopping services..."
# Attempt to enable maintenance mode, but proceed if container is already down
set_maintenance_mode "--on" || true
# Stop Nextcloud and Homeassistant service
docker compose $(get_compose_args) stop nextcloud homeassistant vaultwarden 2>/dev/null || \
    docker compose $(get_compose_args) stop nextcloud homeassistant

# --- 1. Restore Nextcloud Data ---
if [ "$HAS_NC_DATA" = true ]; then
    log_info "Restoring Nextcloud Data..."
    SRC="$TMP_DIR/nc_data"; [[ ! -d "$SRC" ]] && SRC="$TMP_DIR/data"

    # Archives never contain the "replica" landing area (see backup.sh) — the
    # exclude keeps --delete from destroying another HomeBrain's received
    # archives when this box is restored.
    rsync -a --delete --exclude=/replica/ "$SRC/" "$NEXTCLOUD_DATA_DIR/" || die "NC Data RSync failed."
    chown -R 33:33 "$NEXTCLOUD_DATA_DIR"
fi

# --- 2. Restore Home Assistant Config ---
if [ "$HAS_HA_CONFIG" = true ]; then
    log_info "Restoring Home Assistant Config..."
    # Ensure volume exists by creating the container (no start)
    docker compose $(get_compose_args) up --no-start homeassistant
    HA_CID=$(get_ha_cid)
    if [[ -z "$HA_CID" ]]; then die "Home Assistant container ID not found. Check if the service exists."; fi
    # Use helper to copy data INTO the named volume
    # This ensures files inside the volume are owned by root (default for HA docker)
    docker run --rm --volumes-from "$HA_CID" \
    -v "$TMP_DIR/ha_config":/restore_src:ro \
    alpine sh -c "rm -rf /config/* && cp -a /restore_src/. /config/" || die "HA restore failed."
fi

# ── Restore OpenClaw ────────────────────────────────────────────────────────
if [[ "${HAS_OPENCLAW_CONFIG}" == "true" ]]; then
    log_info "Restoring OpenClaw config..."
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw"
    cp "${TMP_DIR}/openclaw_config/openclaw.json" "${HOMEBRAIN_HOME}/.openclaw/"
    chmod 600 "${HOMEBRAIN_HOME}/.openclaw/openclaw.json"
    chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "${HOMEBRAIN_HOME}/.openclaw/openclaw.json"
    log_info "OpenClaw config restored."
fi

if [[ "${HAS_OPENCLAW_WORKSPACE}" == "true" ]]; then
    log_info "Restoring OpenClaw workspace..."
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw/workspace"
    rsync -a --delete --quiet \
        "${TMP_DIR}/openclaw_workspace/" \
        "${HOMEBRAIN_HOME}/.openclaw/workspace/"
    chown -R "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "${HOMEBRAIN_HOME}/.openclaw/workspace"
    log_info "OpenClaw workspace restored."
fi

# Integration credentials (HA token, NC app password, email accounts, self
# bearer token, vault session, pending consent state). Mode-preserved.
if [[ -d "${TMP_DIR}/openclaw_integrations" ]]; then
    log_info "Restoring OpenClaw integration credentials..."
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw"
    for f in ha.token nextcloud.token homebrain.token vault.session \
             ha_accounts.json nc_accounts.json email_accounts.json \
             ha_watchers.json ha_watch_pings.json pending_actions.json; do
        [[ -f "${TMP_DIR}/openclaw_integrations/${f}" ]] || continue
        cp -a "${TMP_DIR}/openclaw_integrations/${f}" "${HOMEBRAIN_HOME}/.openclaw/${f}"
        chmod 600 "${HOMEBRAIN_HOME}/.openclaw/${f}"
        chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "${HOMEBRAIN_HOME}/.openclaw/${f}"
    done
    log_info "Integration credentials restored."
    # Watcher last-state is not in the archive (by design: restore must
    # seed quietly, not replay every entity as a fresh on). Drop it and
    # bounce the daemon so it re-seeds from HA.
    rm -f /var/lib/homebrain/ha_watch_state.json
    systemctl try-restart homebrain-ha-watch.service 2>/dev/null || true
    # homebrain.token above came from the SOURCE box, where it was derived from
    # that box's MASTER_PASSWORD. This box keeps its own master password (only
    # the nonce is portable, see the instance-secret merge earlier), so the
    # restored copy is wrong on any cross-box restore. Re-derive it locally.
    refresh_self_token
fi

# Per-integration audit logs (best-effort; the live log path is owned by
# root so we restore into /var/log/homebrain/ directly).
if [[ -d "${TMP_DIR}/mcp_audit" ]]; then
    mkdir -p /var/log/homebrain
    cp -a "${TMP_DIR}/mcp_audit/." /var/log/homebrain/ || true
    log_info "MCP audit logs restored."
fi

# --- 2.5 Restore Nextcloud Apps (If Present) ---
if [ "$HAS_NC_APPS" = true ]; then
    log_info "Restoring Nextcloud Custom User Apps..."
    
    # Use existing helper and robust fallback
    NC_CID=$(get_nc_cid)
    NC_VOL=""
    
    if [[ -n "$NC_CID" ]]; then
        # Try to extract the volume name dynamically from the container
        NC_VOL=$(docker inspect "$NC_CID" --format '{{ range .Mounts }}{{ if eq .Destination "/var/www/html" }}{{ .Name }}{{ end }}{{ end }}' || true)
    fi
    
    # Fallback: If container doesn't exist or inspect failed, search volumes
    if [[ -z "$NC_VOL" ]]; then
        NC_VOL=$(docker volume ls -q | grep "nextcloud_html" | head -n1 || true)
    fi
    
    if [[ -z "$NC_VOL" ]]; then die "Could not locate Nextcloud volume (nextcloud_html)."; fi
    
    # Restore specifically to /custom_apps
    docker run --rm -v "${NC_VOL}:/volume" -v "$TMP_DIR/nc_apps:/restore_src:ro" alpine \
        sh -c "mkdir -p /volume/custom_apps && cp -a /restore_src/. /volume/custom_apps/" || die "Error restoring Nextcloud apps"
fi

# --- 3. Restore Nextcloud Config ---
if [ "$HAS_NC_CONFIG" = true ]; then
    log_info "Restoring Nextcloud Config..."
    SRC="$TMP_DIR/nc_config"; [[ ! -d "$SRC" ]] && SRC="$TMP_DIR/config"
    
    # Reuse the same robust volume logic
    NC_CID=$(get_nc_cid)
    NC_VOL=""
    
    if [[ -n "$NC_CID" ]]; then
        NC_VOL=$(docker inspect "$NC_CID" --format '{{ range .Mounts }}{{ if eq .Destination "/var/www/html" }}{{ .Name }}{{ end }}{{ end }}' || true)
    fi
    
    if [[ -z "$NC_VOL" ]]; then
        NC_VOL=$(docker volume ls -q | grep "nextcloud_html" | head -n1 || true)
    fi
    
    if [[ -z "$NC_VOL" ]]; then die "Could not locate Nextcloud volume for config restore."; fi
    
    docker run --rm -v "${NC_VOL}:/volume" -v "$SRC:/restore_src:ro" alpine \
        sh -c "rm -rf /volume/config/* && cp -a /restore_src/. /volume/config/" || die "Error restoring Nextcloud config.php"
fi

# --- Consolidated DB Password Handling (After Config Restore, Before DB Restore) ---
if [ "$HAS_NC_CONFIG" = true ]; then
    log_info "Extracting restored DB credentials and syncing..."
    # Reuse NC_VOL determined in previous step. If Config was true, NC_VOL is guaranteed set.
    # If Config was false, we skip this block anyway.
    
    # Extract from the restored volume using PHP for safe parsing (final state)
    DB_USER=$(docker run --rm -v "${NC_VOL}:/volume:ro" php:8-cli php -r '
        @include "/volume/config/config.php"; echo $CONFIG["dbuser"] ?? "";
    ') || DB_USER="$MYSQL_USER"  # Fallback to env if extraction fails
    
    DB_PASS=$(docker run --rm -v "${NC_VOL}:/volume:ro" php:8-cli php -r '
        @include "/volume/config/config.php"; echo $CONFIG["dbpassword"] ?? "";
    ')

    if [[ -z "$DB_PASS" ]]; then
        log_warn "No dbpassword found in config.php. Skipping password sync. This may lead to startup failure."
    else
        # Update .env with restored password
        log_info "Updating .env file with restored DB password."
        
        sed -i "s/^MYSQL_PASSWORD=.*/MYSQL_PASSWORD=$DB_PASS/" "$ENV_FILE" || log_warn "Failed to update .env MYSQL_PASSWORD."
        # Sync DB user credentials (always, even if no DB restore)
        log_info "Syncing Database User Credentials..."
        # Start DB if not already (for sync)
        docker compose $(get_compose_args) up -d db
        wait_for_healthy "db" 60 || die "DB failed to start."
        DB_CID=$(get_nc_db_cid)
        docker run --rm \
          --network container:"$DB_CID" \
          -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
          mysql:8 \
          mysql -h 127.0.0.1 -u root -e "ALTER USER '$DB_USER'@'%' IDENTIFIED BY '$DB_PASS'; FLUSH PRIVILEGES;" || log_warn "Failed to sync DB password. Nextcloud may be unhealthy."
    fi
fi

# --- 4. Restore Database ---
if [ "$HAS_NC_DB" = true ]; then
    log_info "Restoring Nextcloud Database..."
    # DB should already be up from password sync or start here
    docker compose $(get_compose_args) up -d db
    wait_for_healthy "db" 60 || die "DB failed to start."
    DB_CID=$(get_nc_db_cid)
    
    # 4a. Import SQL
    SQL_FILE=$(find "$TMP_DIR" -name "*.sql" | head -n 1)
    if [[ -f "$SQL_FILE" ]]; then
        log_info "Importing SQL Dump..."
        docker run --rm \
          --network container:"$DB_CID" \
          -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
          -v "$(dirname "$SQL_FILE"):/restore_dir:ro" \
          mysql:8 \
          sh -c "mysql -h 127.0.0.1 -u root -e 'DROP DATABASE IF EXISTS $MYSQL_DATABASE; CREATE DATABASE $MYSQL_DATABASE;' && mysql -h 127.0.0.1 -u root $MYSQL_DATABASE < /restore_dir/$(basename "$SQL_FILE")" || die "DB Import failed."
    fi

    if [[ -n "${NEXTCLOUD_ADMIN_PASSWORD:-}" ]]; then
        log_info "Synchronizing Nextcloud Admin password to match current environment..."
        
        # We need the container running to run 'occ'
        docker compose --env-file "$ENV_FILE" $(get_compose_args) up -d nextcloud
        wait_for_healthy "nextcloud" 120 || log_warn "NC failed to start for password sync."

        NC_CID=$(get_nc_cid)
        if [[ -n "$NC_CID" ]]; then
             # Reset password using OC_PASS environment variable
             docker exec -u www-data -e OC_PASS="$NEXTCLOUD_ADMIN_PASSWORD" "$NC_CID" \
                php occ user:resetpassword --password-from-env admin || \
                log_warn "Failed to sync Nextcloud password. You may need to use the password from the backup."
        fi
    else
        log_warn "NEXTCLOUD_ADMIN_PASSWORD not set in .env. Retaining password from backup."
    fi

fi

# ── Restore Vault ───────────────────────────────────────────────────────────
if [[ "$HAS_VAULT_DATA" == "true" ]]; then
    log_info "Restoring Vault data directory..."
    VAULT_DATA="${VAULT_DATA_DIR:-${HOMEBRAIN_HOME}/vault-data}"
    mkdir -p "$VAULT_DATA"
    rsync -a --delete "${TMP_DIR}/vault_data/" "$VAULT_DATA/" || die "Vault data restore failed."
    chown -R 65534:65534 "$VAULT_DATA" 2>/dev/null || true
fi

if [[ "$HAS_VAULT_DB" == "true" ]]; then
    log_info "Restoring Vault database..."
    docker compose $(get_compose_args) up -d db
    wait_for_healthy "db" 60 || die "DB failed to start for vault restore."
    DB_CID=$(get_nc_db_cid)
    # Re-create database from scratch using stored creds
    if [[ -n "${VAULT_DB_NAME:-}" ]] && [[ -n "${VAULT_DB_USER:-}" ]] && [[ -n "${VAULT_DB_PASSWORD:-}" ]]; then
        docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$DB_CID" \
            mariadb -u root -e "
            DROP DATABASE IF EXISTS \`${VAULT_DB_NAME}\`;
            CREATE DATABASE \`${VAULT_DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
            CREATE USER IF NOT EXISTS '${VAULT_DB_USER}'@'%' IDENTIFIED BY '${VAULT_DB_PASSWORD}';
            ALTER USER '${VAULT_DB_USER}'@'%' IDENTIFIED BY '${VAULT_DB_PASSWORD}';
            GRANT ALL PRIVILEGES ON \`${VAULT_DB_NAME}\`.* TO '${VAULT_DB_USER}'@'%';
            FLUSH PRIVILEGES;" 2>&1 | grep -v "Using a password" || true
        docker run --rm \
          --network container:"$DB_CID" \
          -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
          -v "${TMP_DIR}/vault_db:/restore_dir:ro" \
          mysql:8 \
          sh -c "mysql -h 127.0.0.1 -u root ${VAULT_DB_NAME} < /restore_dir/vaultwarden.sql" \
          || die "Vault DB import failed."
    else
        die "Vault DB credentials missing in .env — cannot restore vault database."
    fi
fi

# --- Restart ---
# The archive's VAULT_LAN_IP belongs to whatever LAN the backup was taken on,
# which on a bare-metal restore onto new hardware is usually not this one. Fix
# it before caddy is created, so its TLS cert carries the address this box
# actually answers on. See common.sh:refresh_vault_lan_ip.
refresh_vault_lan_ip

log_info "Restarting Docker Stack..."
profiles=$(get_tunnel_profiles)
docker compose --env-file "$ENV_FILE" $(get_compose_args) ${profiles} up -d --remove-orphans || log_error "Failure restarting docker stack."

wait_for_healthy "nextcloud" 180 || log_warn "Nextcloud taking longer to start."
if [ "$HAS_HA_CONFIG" = true ]; then
wait_for_healthy "homeassistant" 120 || log_warn "HomeAssistant taking longer to start."
fi

# Re-apply Proxy/Tunnel Configuration ---
# The restored config.php contains OLD trusted_domains/proxies from the backup time.
# We must overwrite them with the CURRENT environment settings immediately.
log_info "Updating restored config with current Tunnel and Proxy settings..."

# Safety: Ensure defaults exist if missing in .env to prevent 'set -u' crash
export TRUSTED_PROXIES_0="${TRUSTED_PROXIES_0:-127.0.0.1}"
export TRUSTED_PROXIES_1="${TRUSTED_PROXIES_1:-172.16.0.0/12}"

configure_nc_ha_proxy_settings || log_warn "Failed to apply proxy settings. External access might be broken."

# Restart to apply proxy settings (Safe restart)
# We do not restart DB here, only the frontends
log_info "Restarting NC & HA frontends to apply proxy settings."
docker compose $(get_compose_args) restart nextcloud homeassistant
wait_for_healthy "nextcloud" 120 || log_error "Nextcloud failed to get healthy after proxy config" 
wait_for_healthy "homeassistant" 120 || log_error "Homeassistant failed to get healthy after proxy config" 

if [[ "$HAS_VAULT_DB" == "true" ]] || [[ "$HAS_VAULT_DATA" == "true" ]]; then
    wait_for_healthy "vaultwarden" 120 || die "Vaultwarden failed to become healthy after restore."
    if [[ "$HAS_VAULT_DB" == "true" ]]; then
        DB_CID=$(get_nc_db_cid)
        count="$(docker exec -e MYSQL_PWD="${VAULT_DB_PASSWORD}" "$DB_CID" \
            mariadb -u "${VAULT_DB_USER}" -N -s -e \
            "SELECT COUNT(*) FROM \`${VAULT_DB_NAME}\`.users;" 2>/dev/null)" \
            || die "Vault DB restored but the users table is missing or unreadable."
        if ! [[ "$count" =~ ^[0-9]+$ ]]; then
            die "Vault DB restored but the users table is missing or unreadable."
        fi
        log_info "Vault restored with ${count} user(s)."
    fi
fi

# Member escrow and the vault DB are one unit (HOUSEHOLD_ACCOUNTS.md §6.4).
# Restore the json iff the vault DB came back; re-wrap under dest's
# RECOVERY_BACKUP_KEY; never leave member_escrow.wrap on dest.
ESCROW_DEST="/var/lib/homebrain/member_escrow.json"
rm -f /var/lib/homebrain/member_escrow.wrap
if [[ "$HAS_VAULT_DB" == "true" && "$HAS_ESCROW" == "true" ]]; then
    if [[ -z "${RECOVERY_BACKUP_KEY:-}" ]]; then
        die "Backup unlock is not enabled on this box — member vault recovery cannot be restored. Enable it, then restore again. Nothing was left of the wrap file."
    fi
    mkdir -p /var/lib/homebrain
    if [[ "$HAS_ESCROW_WRAP" == "true" ]]; then
        DEST_KEY_FILE="${TMP_DIR}/.dest_escrow_key"
        printf '%s' "$RECOVERY_BACKUP_KEY" > "$DEST_KEY_FILE"
        chmod 600 "$DEST_KEY_FILE"
        "$(backup_crypto_python)" "${INSTALL_DIR}/src/member_escrow.py" restore-rewrap \
            --json "${TMP_DIR}/member_escrow.json" \
            --wrap-file "${TMP_DIR}/member_escrow.wrap" \
            --dest-key-file "$DEST_KEY_FILE" \
            --out "$ESCROW_DEST" \
            || die "Could not re-wrap member vault recovery onto this box."
        rm -f "$DEST_KEY_FILE" "${TMP_DIR}/member_escrow.wrap"
        chmod 600 "$ESCROW_DEST"
        log_info "Member vault recovery restored and re-wrapped for this box."
    else
        cp "${TMP_DIR}/member_escrow.json" "$ESCROW_DEST" || die "Could not restore member vault recovery."
        chmod 600 "$ESCROW_DEST"
        log_warn "Archive had member escrow but no wrap file — copied as-is. If this is a new box, issued vault passwords cannot be reset."
    fi
elif [[ "$HAS_VAULT_DB" == "true" && "$HAS_ESCROW" == "false" ]]; then
    log_warn "Vault restored without member recovery escrow — HomeBrain cannot reset issued vault passwords from this archive."
elif [[ "$HAS_VAULT_DB" == "false" && "$HAS_ESCROW" == "true" ]]; then
    log_warn "Archive has member vault recovery but no vault database — restoring neither (they are one unit)."
fi
rm -f /var/lib/homebrain/member_escrow.wrap

# --- Home Assistant admin password -----------------------------------------
# The mirror of the Nextcloud sync above, which HA never had. Restoring HA's
# config restores its auth store, so the box comes back answering to whatever
# password the *archive* was made under — while .env, the dashboard, Nextcloud
# and the password the owner was just shown all say something else. On a
# bare-metal restore that is the only password they have, and Home Assistant is
# the one service that refuses it.
#
# Found by the self-test on a restored box: dashboard ok, Nextcloud ok, "Home
# Assistant rejected the recorded password" — an inconsistency that had been
# sitting there since the previous restore, unnoticed because nothing asked.
#
# Only for an account HomeBrain manages. Where Home Assistant keeps its own
# password, the archive's auth store is the owner's own and forcing .env's
# value over it would be HomeBrain overwriting a password it never set — the
# restore would take away the login they have been using.
if [[ -n "${HA_ADMIN_PASSWORD:-}" ]]; then
    log_info "Synchronizing Home Assistant admin password to match current environment..."
    # `|| ha_rc=$?`, not a bare call: under `set -e` a bare call that returns
    # non-zero ends the restore on the spot, and "Home Assistant manages its
    # own password" is a perfectly ordinary answer here.
    ha_rc=0
    ha_sync_admin_password "$HA_ADMIN_PASSWORD" || ha_rc=$?
    case "$ha_rc" in
        0)  log_info "Home Assistant accepts the current password." ;;
        3)  log_info "Home Assistant manages its own password — restored as it was in the backup." ;;
        2)  log_warn "Could not read which account owns Home Assistant — its password was left as the backup had it." ;;
        *)  log_warn "Home Assistant kept the password from the backup — the master password will NOT open it."
            log_warn "Change it in HA → Profile, or run scripts/rotate_master_password.sh (non-fatal)." ;;
    esac
else
    log_warn "HA_ADMIN_PASSWORD not set in .env — Home Assistant keeps the password from the backup."
fi

log_info "Disabling maintenance mode"
set_maintenance_mode "--off"

# Trigger Repairs/Scan
if [ "$HAS_NC_DATA" = true ]; then
    log_info "Running post-restore upgrade if needed..."
    docker compose $(get_compose_args) exec -u www-data nextcloud php occ upgrade || log_warn "Upgrade failed—check Nextcloud logs."
    
    log_info "Running post-restore repairs..."
    
    docker compose $(get_compose_args) exec -u www-data nextcloud php occ maintenance:repair || log_warn "Repair failed."
    docker compose $(get_compose_args) exec -u www-data nextcloud php occ db:add-missing-indices || log_warn "Index add failed."
    
    log_info "Triggering Nextcloud data scan for all users"
    docker exec -u www-data "$(get_nc_cid)" php occ files:scan --all || log_error "Nextcloud file scan failed."
fi

log_info "=== Restore Complete From: $BACKUP_FILE ==="
rm -rf "$TMP_DIR"
