#!/bin/bash
#
# Move the Nextcloud data directory onto a dedicated drive.
#
#   move_nc_data.sh /dev/sda
#
# Why this exists: a production box filled its root disk with 426 GB of camera
# footage and the only resolution on offer was "delete the footage". The box
# could already format and mount a second drive; it just had no way to put the
# files there.
#
# The drive is mounted AT the data directory rather than under it. That is
# deliberate: Nextcloud refuses to start when its data directory has no
# .ncdata marker, so if the drive ever fails to appear, Nextcloud stops loudly
# instead of quietly writing a second, divergent copy onto the root disk.
#
# Every step is idempotent. This moves hundreds of gigabytes over USB and will
# be interrupted; re-running continues where it stopped. Nothing is deleted
# from the source until the copy has been verified byte-for-byte AND Nextcloud
# has come back healthy on the new drive.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

DEST="/mnt/nextcloud-data"
LABEL="NextcloudData"

[[ $EUID -eq 0 ]] || die "Must run as root."

DEV="${1:-}"
[[ -n "$DEV" ]] || die "Usage: move_nc_data.sh /dev/sdX"
[[ -b "$DEV" ]] || die "$DEV is not a block device."

load_env
SRC="${NEXTCLOUD_DATA_DIR:-${HOMEBRAIN_HOME}/nextcloud-data}"

# --- 1. Pre-flight ------------------------------------------------------
# Refuse before touching anything, not halfway through.

[[ "$SRC" != "$DEST" ]] || die "Nextcloud data is already on $DEST."
[[ -d "$SRC" ]] || die "Current data directory $SRC does not exist."

root_src=$(findmnt -n -o SOURCE / | sed 's/\[.*\]//')
root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null || true)
[[ -z "$root_disk" ]] || [[ "/dev/$root_disk" != "$DEV" ]] \
    || die "$DEV holds the root filesystem."
[[ "$DEV" != "$root_src" ]] || die "$DEV is the root filesystem."

# The backup drive and the data drive must not be the same spindle: a single
# failure would take the files and every archive of them at once.
backup_src=$(findmnt -n -o SOURCE "$BACKUP_MOUNTDIR" 2>/dev/null || true)
[[ -z "$backup_src" ]] || [[ "$backup_src" != "$DEV"* ]] \
    || die "$DEV is the backup drive. Use a different drive for files."

src_kb=$(du -sk "$SRC" | awk '{print $1}')
dev_kb=$(($(blockdev --getsize64 "$DEV") / 1024))
# 5% headroom for filesystem overhead on the target.
[[ $dev_kb -gt $((src_kb * 105 / 100)) ]] \
    || die "$DEV holds ${dev_kb}KB; the data directory needs ${src_kb}KB plus overhead."

log_info "Moving Nextcloud data: $SRC → $DEST (on $DEV)"

# --- 2. Prepare the drive (skipped when resuming) -----------------------
# The label is how a re-run recognises its own work. Formatting a drive that
# already carries a half-finished copy would throw the copy away.
current_label=$(blkid -o value -s LABEL "$DEV" 2>/dev/null || true)
if [[ "$current_label" == "$LABEL" ]]; then
    log_info "$DEV is already prepared — resuming."
else
    log_info "Formatting $DEV (ext4, label $LABEL)"
    umount "$DEV"* 2>/dev/null || true
    wipefs -a "$DEV"
    mkfs.ext4 -F -L "$LABEL" "$DEV"
    udevadm settle
fi

uuid=$(blkid -o value -s UUID "$DEV")
[[ -n "$uuid" ]] || die "Could not read a UUID from $DEV after formatting."

mkdir -p "$DEST"
# nofail so a missing drive never blocks boot, and a device timeout because USB
# enumeration is slower than systemd's default patience on some boxes.
sed -i "\|[[:space:]]${DEST}[[:space:]]|d" /etc/fstab
echo "UUID=$uuid $DEST ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2" >> /etc/fstab
systemctl daemon-reload 2>/dev/null || true
mountpoint -q "$DEST" || mount "$DEST"
mountpoint -q "$DEST" || die "$DEST did not mount."
chown 33:33 "$DEST"   # www-data inside the container

# --- 3. Copy ------------------------------------------------------------
# Pass 1 runs with Nextcloud live. On a large library the bulk copy takes
# hours, and taking the whole system offline for that is not something an
# owner would ever risk. Pass 2 runs under maintenance mode and only has to
# carry whatever changed during pass 1, so the outage is minutes.
#
# --partial keeps a half-transferred large file as the basis for the next run
# instead of starting it again. lost+found is the filesystem's, not ours, and
# would otherwise show up as a difference in the verification below.
RSYNC_OPTS=(-aHAX --delete --partial --exclude=/lost+found)

run_rsync() {
    # progress2 redraws one line with \r. Split it into lines and keep every
    # hundredth (~10s apart) so the dashboard log view shows live progress
    # without the file growing to gigabytes on a long transfer.
    rsync "${RSYNC_OPTS[@]}" --info=progress2 "$SRC/" "$DEST/" 2>&1 \
        | stdbuf -oL tr '\r' '\n' \
        | awk 'NR % 100 == 0 { print } END { print }'
}

# Resolved per call, not once: recreating the container changes its id, and a
# stale one here would silently leave Nextcloud in maintenance mode.
maintenance() {
    local cid
    cid=$(get_nc_cid)
    [[ -n "$cid" ]] || return 0
    docker exec -u www-data "$cid" php occ maintenance:mode "--$1" 2>/dev/null \
        || log_warn "Could not set maintenance mode $1."
}

log_info "Copy pass 1 of 2 (Nextcloud stays up)"
run_rsync

log_info "Copy pass 2 of 2 (Nextcloud offline)"
maintenance on
run_rsync

# --- 4. Verify the copy before trusting it ------------------------------
# A dry run that reports nothing left to do is the honest proof. Anything at
# all here means the two trees differ, and we stop with the source intact.
diffs=$(rsync "${RSYNC_OPTS[@]}" -n --itemize-changes "$SRC/" "$DEST/" 2>&1 || true)
if [[ -n "$diffs" ]]; then
    maintenance off
    log_error "$diffs"
    die "Copy verification failed — $SRC is untouched. Re-run to continue."
fi
# Nextcloud 31 renamed the data-directory marker .ocdata → .ncdata and still
# accepts either. Both, because this box has shipped both.
[[ -f "$DEST/.ncdata" || -f "$DEST/.ocdata" ]] \
    || { maintenance off; die "No .ncdata marker in $DEST — refusing to switch."; }
log_info "Verified: $DEST matches $SRC"

# --- 5. Switch over -----------------------------------------------------
# Only the host side of the bind mount moves. Inside the container the data
# directory is /var/www/html/data either way, so config.php needs no edit and
# the file cache stays valid — no rescan, on a library where a rescan would
# take hours.
update_env_var "NEXTCLOUD_DATA_DIR" "$DEST"
# load_env exported the OLD path into this shell, and Compose resolves
# ${NEXTCLOUD_DATA_DIR} from the environment before it looks at --env-file.
# Without this the container is recreated on the directory we just left.
export NEXTCLOUD_DATA_DIR="$DEST"

recreate_nextcloud() {
    # --force-recreate: a plain `up -d` reuses the running container's mount
    # config and would leave Nextcloud pointed at the old directory.
    # shellcheck disable=SC2046  # get_compose_args is intentionally word-split
    ( cd "$INSTALL_DIR" && docker compose --env-file "$ENV_FILE" $(get_compose_args) \
        up -d --force-recreate nextcloud )
}

# Put the box back exactly as it was. Reached only before anything is deleted,
# so reverting the pointer and the container is a complete undo.
revert_and_die() {
    log_error "$1"
    update_env_var "NEXTCLOUD_DATA_DIR" "$SRC"
    export NEXTCLOUD_DATA_DIR="$SRC"
    recreate_nextcloud || log_error "Could not restore Nextcloud on $SRC — run deploy.sh."
    maintenance off
    die "Move abandoned. Nextcloud is back on $SRC with all data intact."
}

log_info "Recreating Nextcloud on the new mount"
recreate_nextcloud || revert_and_die "Nextcloud failed to recreate on $DEST."
wait_for_healthy "nextcloud" 300 || revert_and_die "Nextcloud did not come back healthy on $DEST."

nc_cid=$(get_nc_cid)
mounted_from=$(docker inspect -f \
    '{{range .Mounts}}{{if eq .Destination "/var/www/html/data"}}{{.Source}}{{end}}{{end}}' \
    "$nc_cid")
[[ "$mounted_from" == "$DEST" ]] \
    || revert_and_die "Nextcloud is still mounted from $mounted_from."
maintenance off

# --- 6. Follow-on paths that embed the old location ---------------------
# Camera FTP accounts write into the data directory by absolute path; the
# watcher script and per-user configs were generated with the old one.
if compgen -G "/etc/vsftpd/user_conf/*" >/dev/null 2>&1; then
    log_info "Repointing camera FTP accounts at $DEST"
    sed -i "s|^local_root=${SRC}/|local_root=${DEST}/|" /etc/vsftpd/user_conf/* || true
    [[ -f /usr/local/bin/nextcloud-ftp-sync.sh ]] \
        && sed -i "s|${SRC}/|${DEST}/|g" /usr/local/bin/nextcloud-ftp-sync.sh
    systemctl restart vsftpd 2>/dev/null || true
    systemctl restart 'nextcloud-ftp-sync@*' 2>/dev/null || true
fi

# --- 7. Reclaim the old copy -------------------------------------------
# The whole point of the move. Reached only after the copy verified clean and
# Nextcloud served requests from the new drive.
log_info "Removing the old copy at $SRC"
find "$SRC" -mindepth 1 -delete

log_info "=== Nextcloud data now lives on $DEST ($(df -h --output=size "$DEST" | tail -1 | tr -d ' ')) ==="
