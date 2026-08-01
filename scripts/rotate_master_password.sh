#!/bin/bash
# rotate_master_password.sh — rotate the box's master password across every
# service that derives from it, then re-derive the dependent secrets.
#
# Invoked by the dashboard recovery-reset flow (src/app.py). The NEW password
# arrives via a 0600 file whose path is argv $1 — never on the command line,
# so it can't leak through `ps`/journald — and the file is shredded on exit.
#
# Safety model (this is the important part):
#   * The CURRENT (old) per-service passwords are read from .env. Each running
#     service is re-credentialed FIRST; its .env key is rewritten only after
#     the live change is confirmed — so .env never gets ahead of reality.
#   * MariaDB is the one data-critical, abort-on-failure step: if the root
#     rotation or its re-verification fails we `die` BEFORE touching .env, so
#     the box is left exactly as it was.
#   * The internal Nextcloud<->DB password (MYSQL_PASSWORD / nextcloud_user) is
#     intentionally NOT rotated: it is plumbing the user never types, and
#     rotating it would deadlock `occ` (occ needs DB access to rewrite its own
#     stored DB password). Leaving it untouched keeps NC<->DB consistent.
#   * Home Assistant and the derived Vault/OpenClaw tokens are best-effort:
#     failure is logged but never strands the box (worst case those keep their
#     old secret, changeable from their own UIs).
#
# NB: not `set -e` — every step's failure is handled explicitly so a single
# non-critical hiccup can't abort a partially-applied rotation.
set -uo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

# Minimal copy of utilities.sh:run_as_admin — avoids sourcing utilities.sh just
# for one helper (and its top-level side effects). Runs a command as the
# homebrain user with a working user-systemd session.
run_as_admin() {
    local hb_uid
    hb_uid=$(id -u "${HOMEBRAIN_USER}")
    loginctl enable-linger "${HOMEBRAIN_USER}" 2>/dev/null || true
    mkdir -p "/run/user/${hb_uid}" 2>/dev/null || true
    chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "/run/user/${hb_uid}" 2>/dev/null || true
    sudo -u "${HOMEBRAIN_USER}" \
        XDG_RUNTIME_DIR="/run/user/${hb_uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${hb_uid}/bus" \
        "$@"
}

SECRETS_FILE="${1:-}"
[[ -n "$SECRETS_FILE" && -f "$SECRETS_FILE" ]] || die "usage: rotate_master_password.sh <new-password-file>"
cleanup() { shred -u "$SECRETS_FILE" 2>/dev/null || rm -f "$SECRETS_FILE" 2>/dev/null || true; }
trap cleanup EXIT

NEW_PASS="$(head -n1 "$SECRETS_FILE")"
[[ -n "$NEW_PASS" ]] || die "new password file is empty"
# Defense in depth: the dashboard validates this charset, but never trust input
# crossing a process boundary. The set excludes quotes/backslash/whitespace so
# the value is injection-safe in the SQL literal and shell calls below.
if ! [[ "$NEW_PASS" =~ ^[A-Za-z0-9][A-Za-z0-9_.@!#%+=:?-]{7,127}$ ]]; then
    die "new password fails safe-charset policy; aborting rotation"
fi

load_env
log_info "=== Master password rotation starting ==="

# --- 1. MariaDB root (CRITICAL: abort before any .env change on failure) ---
DB_CID="$(get_nc_db_cid 2>/dev/null || true)"
if [[ -n "$DB_CID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$DB_CID" 2>/dev/null)" == "true" ]]; then
    OLD_ROOT="${MYSQL_ROOT_PASSWORD:-}"
    : "${OLD_ROOT:?MYSQL_ROOT_PASSWORD missing in .env}"
    log_info "Rotating MariaDB root credential..."
    if ! docker exec -e MYSQL_PWD="$OLD_ROOT" "$DB_CID" mariadb -u root -e "
        ALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED BY '${NEW_PASS}';
        ALTER USER IF EXISTS 'root'@'%'        IDENTIFIED BY '${NEW_PASS}';
        FLUSH PRIVILEGES;
    "; then
        die "MariaDB root rotation failed — aborting before any .env change. Box unchanged."
    fi
    if ! docker exec -e MYSQL_PWD="$NEW_PASS" "$DB_CID" mariadb -u root -e "SELECT 1;" >/dev/null 2>&1; then
        die "MariaDB new-password verification failed — aborting. Box unchanged."
    fi
    update_env_var "MYSQL_ROOT_PASSWORD" "$NEW_PASS"
    log_info "MariaDB root rotated and verified."
else
    log_warn "DB container not running — skipping MariaDB rotation."
fi

# --- 2. Nextcloud admin login (uses unchanged nextcloud_user DB creds) -----
NC_CID="$(get_nc_cid 2>/dev/null || true)"
if [[ -n "$NC_CID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$NC_CID" 2>/dev/null)" == "true" ]]; then
    log_info "Rotating Nextcloud admin password..."
    if docker exec -u www-data -e OC_PASS="$NEW_PASS" "$NC_CID" \
            php occ user:resetpassword --password-from-env "${NEXTCLOUD_ADMIN_USER:-admin}" >/dev/null 2>&1; then
        update_env_var "NEXTCLOUD_ADMIN_PASSWORD" "$NEW_PASS"
        log_info "Nextcloud admin password rotated."
    else
        log_warn "Nextcloud admin reset failed — NEXTCLOUD_ADMIN_PASSWORD left unchanged (non-fatal)."
    fi
else
    log_warn "Nextcloud container not running — skipping NC admin rotation (non-fatal)."
fi

# --- 3. Home Assistant admin login (BEST-EFFORT) ---------------------------
# Confirms the change by logging in, because neither of the obvious signals is
# trustworthy here:
#   * `-c /config` is NOT optional. The `auth` sub-parser owns that flag and
#     defaults to ~/.homeassistant, which does not exist inside the container —
#     so without it the CLI edits an empty store and changes nothing.
#   * The CLI exits 0 either way. Missing the user prints "User not found" and
#     still returns success, so its status cannot gate the .env write.
# Together those two silently produced the exact state this script's safety
# model exists to prevent: HA_ADMIN_PASSWORD in .env holding a password that
# Home Assistant had never accepted.
# HA also caches the credential in memory, so the change only goes live after a
# restart — hence change, restart, verify, and only then record.
ha_login_works() {
    local pw="$1" flow_id payload
    flow_id=$(curl -s --max-time 10 -H 'Content-Type: application/json' \
        -d '{"client_id":"http://127.0.0.1:8123/","handler":["homeassistant",null],"redirect_uri":"http://127.0.0.1:8123/"}' \
        "http://127.0.0.1:8123/auth/login_flow" 2>/dev/null | jq -r '.flow_id // empty' 2>/dev/null)
    [[ -n "$flow_id" ]] || return 1
    # jq builds the body so the password is JSON-escaped rather than pasted in.
    payload=$(jq -nc --arg p "$pw" \
        '{client_id:"http://127.0.0.1:8123/", username:"admin", password:$p}') || return 1
    curl -s --max-time 10 -H 'Content-Type: application/json' -d "$payload" \
        "http://127.0.0.1:8123/auth/login_flow/${flow_id}" 2>/dev/null \
        | grep -q '"type": *"create_entry"'
}

HA_CID="$(get_ha_cid 2>/dev/null || true)"
if [[ -n "$HA_CID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$HA_CID" 2>/dev/null)" == "true" ]]; then
    log_info "Rotating Home Assistant admin password (best-effort)..."
    if docker exec "$HA_CID" hass --script auth -c /config \
            change_password admin "$NEW_PASS" >/dev/null 2>&1 \
       && docker restart "$HA_CID" >/dev/null 2>&1; then
        # Container health flips before the auth API answers, so poll that.
        for _ in $(seq 1 30); do
            [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
                http://127.0.0.1:8123/api/onboarding 2>/dev/null)" == "200" ]] && break
            sleep 2
        done
        if ha_login_works "$NEW_PASS"; then
            update_env_var "HA_ADMIN_PASSWORD" "$NEW_PASS"
            log_info "Home Assistant password rotated and verified."
        else
            log_warn "HA did not accept the new password — HA_ADMIN_PASSWORD left unchanged in .env."
            log_warn "Home Assistant keeps its old password; change it via HA → Profile (non-fatal)."
        fi
    else
        log_warn "HA auth CLI failed — HA keeps its old password; change it via HA → Profile (non-fatal)."
    fi
else
    log_warn "Home Assistant container not running — skipping (non-fatal)."
fi

# --- 4. Canonical master + dashboard-login password ------------------------
# After the service rotations, so MASTER_PASSWORD reflects the value the tokens
# below are derived from. MANAGER_PASSWORD is login-only; the dashboard may have
# already set it in-process, this makes it authoritative + idempotent.
update_env_var "MASTER_PASSWORD" "$NEW_PASS"
update_env_var "MANAGER_PASSWORD" "$NEW_PASS"
log_info "MASTER_PASSWORD / MANAGER_PASSWORD updated."

# Backups are encrypted with the master password AS IT WAS AT BACKUP TIME
# (backup.sh: gpg stores the s2k salt per archive), so every existing archive
# now needs the OLD password. That is survivable for a deliberate change — the
# user still knows it — but this script is also the recovery-phrase path, whose
# entire premise is that they have forgotten it. Record when the boundary moved
# so the dashboard can mark which archives predate it, instead of leaving the
# user to guess in a passphrase prompt.
mkdir -p /var/lib/homebrain
EPOCH_FILE="/var/lib/homebrain/backup_epoch.json"
printf '{"ts": %d, "rotated_at": "%s"}\n' \
    "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${EPOCH_FILE}.tmp" \
    && mv "${EPOCH_FILE}.tmp" "$EPOCH_FILE"
log_info "Backup epoch recorded — archives older than now need the previous password."

# --- 5. Re-derive Vault admin token (BEST-EFFORT) --------------------------
# ADMIN_TOKEN is an env var the vaultwarden container reads at start, so a
# running container must be recreated to pick up the new .env value.
update_env_var "VAULT_ADMIN_TOKEN" ""   # force provision_vault.sh to re-derive
if bash "$SCRIPT_DIR/provision_vault.sh" >/dev/null 2>&1; then
    log_info "Vault admin token re-derived."
    VAULT_CID="$(get_vault_cid 2>/dev/null || true)"
    if [[ -n "$VAULT_CID" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$VAULT_CID" 2>/dev/null)" == "true" ]]; then
        # shellcheck disable=SC2046  # get_compose_args is intentionally word-split
        if docker compose --env-file "$ENV_FILE" $(get_compose_args) up -d --no-deps --force-recreate vaultwarden >/dev/null 2>&1; then
            log_info "Vaultwarden recreated with new admin token."
        else
            log_warn "Vaultwarden recreate failed — admin SSO needs a redeploy (non-fatal)."
        fi
    fi
else
    log_warn "Vault token re-derivation failed — admin SSO may need a manual re-provision (non-fatal)."
fi

# --- 6. Re-derive OpenClaw gateway token (BEST-EFFORT, GPU boxes only) ------
CFG="${HOMEBRAIN_HOME}/.openclaw/openclaw.json"
if [[ "${HAS_GPU:-false}" == "true" ]] && [[ -f "$CFG" ]] && command -v jq >/dev/null 2>&1; then
    log_info "Re-deriving OpenClaw gateway token..."
    GW_TOKEN="$(printf '%s:openclaw-gateway' "$NEW_PASS" | sha256sum | cut -c1-32)"
    TMP_CFG="$(mktemp)"
    if jq --arg t "$GW_TOKEN" '.gateway.auth.token = $t' "$CFG" > "$TMP_CFG" 2>/dev/null; then
        chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "$TMP_CFG" 2>/dev/null || true
        chmod 600 "$TMP_CFG"
        mv "$TMP_CFG" "$CFG"
        run_as_admin systemctl --user restart openclaw-gateway 2>/dev/null \
            || log_warn "openclaw-gateway restart failed; token applies on next start."
        log_info "OpenClaw gateway token re-derived."
    else
        rm -f "$TMP_CFG"
        log_warn "jq patch of openclaw.json failed — gateway token unchanged (non-fatal)."
    fi
fi

# --- 7. Re-derive the self-MCP bearer token (BEST-EFFORT) ------------------
# Derived from MASTER_PASSWORD, so it goes stale the moment step 4 lands. The
# dashboard only rewrites it on a Connections "Apply", so without this the
# agent's homebrain-self__* tools 401 until someone happens to click that.
refresh_self_token "$NEW_PASS"

# --- 8. Fresh backup under the NEW password ---------------------------------
# Without this the box can sit indefinitely with zero archives the user can
# actually open: every existing one is sealed with the old password, and after
# a recovery-phrase reset that password is gone for good. Detached with its own
# session and fds so the rotation task returns immediately — a full backup can
# run for hours, and the caller (the dashboard's background task) waits on the
# pipe. backup.sh writes its own log and takes the backup lock, so a collision
# with a scheduled run is handled there.
if [[ -x "$SCRIPT_DIR/backup.sh" ]] || [[ -f "$SCRIPT_DIR/backup.sh" ]]; then
    log_info "Starting a full backup under the new password (detached)."
    nohup setsid bash "$SCRIPT_DIR/backup.sh" --strategy full >/dev/null 2>&1 &
    disown 2>/dev/null || true
else
    log_warn "backup.sh not found — take a backup manually; existing archives need the OLD password."
fi

log_info "=== Master password rotation complete ==="
