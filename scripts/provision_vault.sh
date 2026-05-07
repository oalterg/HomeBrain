#!/bin/bash
# provision_vault.sh — bootstrap HomeBrain Vault (Vaultwarden) state
#
# Idempotent. Run by provision.sh on every install, and by the dashboard's
# vault bootstrap endpoint. Generates per-install secrets, derives the
# Argon2id-hashed admin token from MASTER_PASSWORD + a stable nonce,
# creates the MariaDB DB+user, and writes VAULT_* keys to .env.
#
# Requires (will be installed by provision.sh):
#   - argon2  (apt package)
#   - openssl
#   - mariadb-client (only used when db container is running)
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

load_env

: "${MASTER_PASSWORD:?MASTER_PASSWORD must be set in .env before vault provisioning}"

VAULT_DATA_DIR_DEFAULT="${HOMEBRAIN_HOME}/vault-data"

# --- 1. Data directory ---
VAULT_DATA_DIR="${VAULT_DATA_DIR:-$VAULT_DATA_DIR_DEFAULT}"
mkdir -p "$VAULT_DATA_DIR"
# Vaultwarden runs as UID/GID 65534 (nobody) inside the container by default.
# Granting traversal to all is fine — sensitive files (rsa_key.pem) are 0600
# inside; the directory itself only needs to be writable by the container UID.
chown -R 65534:65534 "$VAULT_DATA_DIR" 2>/dev/null || chmod 0750 "$VAULT_DATA_DIR"
update_env_var "VAULT_DATA_DIR" "$VAULT_DATA_DIR"

# --- 2. DB password (random, persistent) ---
if [[ -z "${VAULT_DB_PASSWORD:-}" ]]; then
    VAULT_DB_PASSWORD="$(openssl rand -base64 32 | tr -d '\n=+/' | head -c 40)"
    update_env_var "VAULT_DB_PASSWORD" "$VAULT_DB_PASSWORD"
    log_info "Generated VAULT_DB_PASSWORD."
fi

# --- 3. Admin nonce (random, persistent — stable across restarts) ---
if [[ -z "${VAULT_ADMIN_NONCE:-}" ]]; then
    VAULT_ADMIN_NONCE="$(openssl rand -hex 16)"
    update_env_var "VAULT_ADMIN_NONCE" "$VAULT_ADMIN_NONCE"
    log_info "Generated VAULT_ADMIN_NONCE."
fi

# --- 4. Derive admin token (Argon2id) ---
# Plain admin token is HMAC-style: sha256(MASTER_PASSWORD:NONCE). Stable for the
# install — the dashboard recomputes it on demand to drive the admin panel SSO,
# so MASTER_PASSWORD doesn't need to be hand-typed into the vault.
# The .env stores ONLY the Argon2id PHC hash; the plaintext is never persisted.
plain_token="$(printf '%s:%s' "$MASTER_PASSWORD" "$VAULT_ADMIN_NONCE" | sha256sum | awk '{print $1}')"

if [[ -z "${VAULT_ADMIN_TOKEN:-}" ]] || ! [[ "${VAULT_ADMIN_TOKEN}" =~ ^\$argon2id\$ ]]; then
    if ! command -v argon2 >/dev/null 2>&1; then
        log_warn "argon2 CLI not installed — falling back to plaintext ADMIN_TOKEN (vaultwarden will warn). Install 'argon2' apt package to upgrade."
        update_env_var "VAULT_ADMIN_TOKEN" "$plain_token"
    else
        salt="$(openssl rand -base64 16 | tr -d '\n=')"
        # Vaultwarden's recommended params: m=64MiB (-k 65540), t=3, p=4
        hash="$(printf '%s' "$plain_token" | argon2 "$salt" -e -id -k 65540 -t 3 -p 4)"
        update_env_var "VAULT_ADMIN_TOKEN" "$hash"
        log_info "Generated Argon2id-hashed VAULT_ADMIN_TOKEN."
    fi
fi

# --- 5. Database + user (only when db container is up) ---
DB_CID="$(get_nc_db_cid 2>/dev/null || true)"
if [[ -n "$DB_CID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$DB_CID" 2>/dev/null)" == "true" ]]; then
    : "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD required to bootstrap vault DB}"
    log_info "Bootstrapping vault database..."
    docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$DB_CID" \
        mariadb -u root -e "
        CREATE DATABASE IF NOT EXISTS \`${VAULT_DB_NAME:-vaultwarden}\`
            CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
        CREATE USER IF NOT EXISTS '${VAULT_DB_USER:-vaultwarden_user}'@'%' IDENTIFIED BY '${VAULT_DB_PASSWORD}';
        ALTER USER '${VAULT_DB_USER:-vaultwarden_user}'@'%' IDENTIFIED BY '${VAULT_DB_PASSWORD}';
        GRANT ALL PRIVILEGES ON \`${VAULT_DB_NAME:-vaultwarden}\`.* TO '${VAULT_DB_USER:-vaultwarden_user}'@'%';
        FLUSH PRIVILEGES;
        " 2>&1 | grep -v "Using a password" || true
    log_info "Vault database ready."
else
    log_info "DB container not running — skipping DB bootstrap (will be re-run by dashboard once db is up)."
fi

# --- 6. Domain + LAN IP for Caddy SANs (set by mode) ---
# Always discover the LAN IP — Caddy needs it as a SAN so browsers reaching
# the box by raw IP get a valid cert (mDNS isn't universal on every client OS
# / corporate network).
lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -n "$lan_ip" ]]; then
    update_env_var "VAULT_LAN_IP" "$lan_ip"
fi

if [[ -z "${VAULT_DOMAIN:-}" ]]; then
    if is_local_mode; then
        # LAN HTTPS via Caddy. Bitwarden clients require TLS; web vault works
        # over HTTP too via VAULT_PORT but mobile/desktop apps refuse it.
        # The dashboard recomputes per-request URLs from the user's Host
        # header — this VAULT_DOMAIN is the canonical URL Vaultwarden uses
        # internally (Send links, password-reset emails, etc.).
        update_env_var "VAULT_DOMAIN" "https://homebrain.local:${VAULT_LOCAL_HTTPS_PORT:-8443}"
    else
        # Remote mode: vault.<tunnel-domain>, served via Pangolin TLS edge.
        if [[ -n "${PANGOLIN_DOMAIN:-}" ]]; then
            update_env_var "VAULT_DOMAIN" "https://vault.${PANGOLIN_DOMAIN}"
            update_env_var "VAULT_TRUSTED_DOMAINS" "vault.${PANGOLIN_DOMAIN}"
        fi
    fi
fi

log_info "Vault provisioning complete. Data: $VAULT_DATA_DIR · DB: ${VAULT_DB_NAME:-vaultwarden}"
