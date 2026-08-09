#!/bin/bash
#
# Move the Nextcloud data directory to a different disk.
#
#   move_nc_data.sh /dev/sda      # onto that drive
#   move_nc_data.sh --internal    # back onto the box's own disk
#
# Why this exists: a production box filled its root disk with 426 GB of camera
# footage and the only resolution on offer was "delete the footage". The box
# could already format and mount a second drive; it just had no way to put the
# files there.
#
# Why it moves in both directions: the first version only went internal -> drive,
# once. An owner whose files drive filled up could not move to a bigger one, and
# could not empty a drive they wanted to retire, without a shell. On a product
# whose premise is "no SSH required", the second move is not optional.
#
# The live directory is always a mount point when the files are on a drive,
# never a subdirectory of one. That is deliberate: Nextcloud refuses to start
# when its data directory has no .ncdata marker, so if the drive ever fails to
# appear, Nextcloud stops loudly instead of quietly writing a second, divergent
# copy onto the root disk.
#
# Every step is idempotent. This moves hundreds of gigabytes over USB and will
# be interrupted; re-running continues where it stopped. Nothing is removed from
# the source until the copy has been verified byte-for-byte AND Nextcloud has
# come back healthy on the new location.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPT_DIR/common.sh"

LIVE="/mnt/nextcloud-data"        # where a files drive is always mounted
STAGING="/mnt/nextcloud-data.new" # where it is filled before it becomes LIVE
LABEL="NextcloudData"
# Proof that a half-finished copy on a drive is OURS. Without it, a drive
# carrying another box's library is indistinguishable from our own interrupted
# transfer -- and pass 1 runs with --delete.
RECEIPT=".homebrain-move-in-progress"

[[ $EUID -eq 0 ]] || die "Must run as root."

TARGET="${1:-}"
[[ -n "$TARGET" ]] || die "Usage: move_nc_data.sh /dev/sdX | --internal"

load_env
INTERNAL_DIR="${HOMEBRAIN_HOME}/nextcloud-data"
SRC="${NEXTCLOUD_DATA_DIR:-$INTERNAL_DIR}"

[[ -d "$SRC" ]] || die "Current data directory $SRC does not exist."

# Whether the files are on a drive right now decides three things: which moves
# are legal, whether the old location has to be unmounted, and whether it is
# reclaimed at the end. Resolve it once, up front.
OLD_MOUNT_DEV=""
if mountpoint -q "$SRC"; then
    OLD_MOUNT_DEV=$(findmnt -n -o SOURCE "$SRC" | sed 's/\[.*\]//')
fi

# --- 1. Pre-flight -------------------------------------------------------
# Refuse before touching anything, not halfway through.

src_kb=$(du -sk "$SRC" | awk '{print $1}')

if [[ "$TARGET" == "--internal" ]]; then
    [[ -n "$OLD_MOUNT_DEV" ]] \
        || die "Your files are already on the internal disk ($SRC)."
    DEST="$INTERNAL_DIR"
    # Free space on the root filesystem, not its total size: the OS, the
    # models and the backups are already spending it.
    avail_kb=$(df -Pk "$(dirname "$INTERNAL_DIR")" | awk 'NR==2 {print $4}')
    [[ $avail_kb -gt $((src_kb * 105 / 100)) ]] \
        || die "The internal disk has ${avail_kb}KB free; your files need ${src_kb}KB plus overhead."
    log_info "Moving Nextcloud data: $SRC -> $DEST (internal disk)"
else
    DEV="$TARGET"
    [[ -b "$DEV" ]] || die "$DEV is not a block device."

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

    # Moving a drive onto itself. Reachable now that a second move is allowed.
    [[ -z "$OLD_MOUNT_DEV" ]] || [[ "$OLD_MOUNT_DEV" != "$DEV"* ]] \
        || die "Your files are already on $DEV."

    dev_kb=$(($(blockdev --getsize64 "$DEV") / 1024))
    # 5% headroom for filesystem overhead on the target.
    [[ $dev_kb -gt $((src_kb * 105 / 100)) ]] \
        || die "$DEV holds ${dev_kb}KB; your files need ${src_kb}KB plus overhead."

    DEST="$STAGING"
    log_info "Moving Nextcloud data: $SRC -> $LIVE (on $DEV)"
fi

# --- 2. Prepare the destination -----------------------------------------

mkdir -p "$DEST"

if [[ "$TARGET" != "--internal" ]]; then
    # The label is how a re-run recognises its own work. Formatting a drive
    # that already carries a half-finished copy would throw the copy away.
    current_label=$(blkid -o value -s LABEL "$DEV" 2>/dev/null || true)
    if [[ "$current_label" == "$LABEL" ]]; then
        log_info "$DEV already carries the $LABEL label — inspecting before deciding."
        umount "$DEV"* 2>/dev/null || true
        mount "$DEV" "$DEST" || die "Could not mount $DEV to inspect it."
        existing=$(find "$DEST" -mindepth 1 -maxdepth 1 \
            ! -name lost+found ! -name "$RECEIPT" -print -quit 2>/dev/null || true)
        if [[ -n "$existing" && ! -f "$DEST/$RECEIPT" ]]; then
            # A files drive carried over from another box looks exactly like our
            # own interrupted copy: same label, full of a Nextcloud library. The
            # difference is that we never wrote a receipt to it, and pass 1 below
            # would --delete the lot to match this box's (empty) directory.
            umount "$DEST" 2>/dev/null || true
            die "$DEV already holds a Nextcloud library and this box did not put it there. Refusing to erase it. To bring those files back use Backup & Restore; to reuse the drive, erase it with Format first."
        fi
        log_info "Resuming the previous transfer onto $DEV."
    else
        log_info "Formatting $DEV (ext4, label $LABEL)"
        umount "$DEV"* 2>/dev/null || true
        wipefs -a "$DEV"
        mkfs.ext4 -F -L "$LABEL" "$DEV"
        udevadm settle
        mount "$DEV" "$DEST" || die "Could not mount $DEV at $DEST."
    fi
    mountpoint -q "$DEST" || die "$DEST did not mount."
fi

chown 33:33 "$DEST"   # www-data inside the container
# Written before the first byte and removed only once Nextcloud is serving from
# the new location, so an interruption anywhere in between is recognisable as
# ours on the next run.
printf 'source=%s\nstarted=%s\n' "$SRC" "$(date -u +%FT%TZ)" > "$DEST/$RECEIPT"

# --- 3. Helpers ----------------------------------------------------------
# Defined before the first step that can fail into them.

# --partial keeps a half-transferred large file as the basis for the next run
# instead of starting it again. lost+found is the filesystem's, not ours, and
# the receipt is this script's; both would otherwise show up as differences in
# the verification below.
RSYNC_OPTS=(-aHAX --delete --partial --exclude=/lost+found --exclude="/$RECEIPT")

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

write_fstab_entry() {
    # nofail so a missing drive never blocks boot, and a device timeout because
    # USB enumeration is slower than systemd's default patience on some boxes.
    local uuid="$1"
    [[ -n "$uuid" ]] || return 1
    sed -i "\|[[:space:]]${LIVE}[[:space:]]|d" /etc/fstab
    echo "UUID=$uuid $LIVE ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2" >> /etc/fstab
    systemctl daemon-reload 2>/dev/null || true
}

recreate_nextcloud() {
    # --force-recreate: a plain `up -d` reuses the running container's mount
    # config and would leave Nextcloud pointed at the old directory.
    # shellcheck disable=SC2046  # get_compose_args is intentionally word-split
    ( cd "$INSTALL_DIR" && docker compose --env-file "$ENV_FILE" $(get_compose_args) \
        up -d --force-recreate nextcloud )
}

# Put the box back exactly as it was. Reached only before anything is removed,
# so restoring the mount, the pointer and the container is a complete undo.
revert_and_die() {
    log_error "$1"
    if [[ -n "$OLD_MOUNT_DEV" ]]; then
        umount "$LIVE" 2>/dev/null || true
        if write_fstab_entry "$(blkid -o value -s UUID "$OLD_MOUNT_DEV" 2>/dev/null || true)"; then
            mount "$LIVE" 2>/dev/null || log_error "Could not remount $OLD_MOUNT_DEV at $LIVE."
        else
            log_error "Could not read a UUID from $OLD_MOUNT_DEV — mount $LIVE by hand."
        fi
    fi
    update_env_var "NEXTCLOUD_DATA_DIR" "$SRC"
    export NEXTCLOUD_DATA_DIR="$SRC"
    recreate_nextcloud || log_error "Could not restore Nextcloud on $SRC — run deploy.sh."
    maintenance off
    die "Move abandoned. Nextcloud is back on $SRC with all data intact."
}

# --- 4. Copy -------------------------------------------------------------
# Pass 1 runs with Nextcloud live. On a large library the bulk copy takes
# hours, and taking the whole system offline for that is not something an
# owner would ever risk. Pass 2 runs under maintenance mode and only has to
# carry whatever changed during pass 1, so the outage is minutes.

log_info "Copy pass 1 of 2 (Nextcloud stays up)"
run_rsync

log_info "Copy pass 2 of 2 (Nextcloud offline)"
maintenance on
run_rsync

# --- 5. Verify the copy before trusting it -------------------------------
# A dry run that reports nothing left to do is the honest proof. Anything at
# all here means the two trees differ, and we stop with the source intact.
diffs=$(rsync "${RSYNC_OPTS[@]}" -n --itemize-changes "$SRC/" "$DEST/" 2>&1 || true)
if [[ -n "$diffs" ]]; then
    maintenance off
    log_error "$diffs"
    die "Copy verification failed — $SRC is untouched. Re-run to continue."
fi
# Nextcloud 31 renamed the data-directory marker .ocdata -> .ncdata and still
# accepts either. Both, because this box has shipped both.
[[ -f "$DEST/.ncdata" || -f "$DEST/.ocdata" ]] \
    || { maintenance off; die "No .ncdata marker in $DEST — refusing to switch."; }
log_info "Verified: $DEST matches $SRC"

# --- 6. Switch over ------------------------------------------------------
# Only the host side of the bind mount moves. Inside the container the data
# directory is /var/www/html/data either way, so config.php needs no edit and
# the file cache stays valid — no rescan, on a library where a rescan would
# take hours.

if [[ "$TARGET" == "--internal" ]]; then
    NEW_PATH="$INTERNAL_DIR"
    umount "$SRC" 2>/dev/null || umount -l "$SRC" 2>/dev/null || true
    sed -i "\|[[:space:]]${LIVE}[[:space:]]|d" /etc/fstab
    systemctl daemon-reload 2>/dev/null || true
else
    NEW_PATH="$LIVE"
    uuid=$(blkid -o value -s UUID "$DEV")
    [[ -n "$uuid" ]] || revert_and_die "Could not read a UUID from $DEV."
    # Free the staging mount and, if the files were already on a drive, the old
    # one — both so the new drive can take the canonical path.
    umount "$DEST" || revert_and_die "Could not release the staging mount at $DEST."
    [[ -z "$OLD_MOUNT_DEV" ]] \
        || umount "$SRC" 2>/dev/null || umount -l "$SRC" 2>/dev/null || true
    mkdir -p "$LIVE"
    write_fstab_entry "$uuid" || revert_and_die "Could not write the fstab entry for $DEV."
    mount "$LIVE" || revert_and_die "$LIVE did not mount from $DEV."
    mountpoint -q "$LIVE" || revert_and_die "$LIVE did not mount."
    rmdir "$STAGING" 2>/dev/null || true
fi

update_env_var "NEXTCLOUD_DATA_DIR" "$NEW_PATH"
# load_env exported the OLD path into this shell, and Compose resolves
# ${NEXTCLOUD_DATA_DIR} from the environment before it looks at --env-file.
# Without this the container is recreated on the directory we just left.
export NEXTCLOUD_DATA_DIR="$NEW_PATH"

log_info "Recreating Nextcloud on $NEW_PATH"
recreate_nextcloud || revert_and_die "Nextcloud failed to recreate on $NEW_PATH."
wait_for_healthy "nextcloud" 300 || revert_and_die "Nextcloud did not come back healthy on $NEW_PATH."

nc_cid=$(get_nc_cid)
mounted_from=$(docker inspect -f \
    '{{range .Mounts}}{{if eq .Destination "/var/www/html/data"}}{{.Source}}{{end}}{{end}}' \
    "$nc_cid")
[[ "$mounted_from" == "$NEW_PATH" ]] \
    || revert_and_die "Nextcloud is still mounted from $mounted_from."
maintenance off

# The transfer is complete and serving. Anything left behind from here is
# cleanup, not the move, so the resume marker has done its job.
rm -f "$NEW_PATH/$RECEIPT"

# --- 7. Follow-on paths that embed the old location ----------------------
# Camera FTP accounts write into the data directory by absolute path; the
# watcher script and per-user configs were generated with the old one.
if compgen -G "/etc/vsftpd/user_conf/*" >/dev/null 2>&1; then
    log_info "Repointing camera FTP accounts at $NEW_PATH"
    sed -i "s|^local_root=${SRC}/|local_root=${NEW_PATH}/|" /etc/vsftpd/user_conf/* || true
    [[ -f /usr/local/bin/nextcloud-ftp-sync.sh ]] \
        && sed -i "s|${SRC}/|${NEW_PATH}/|g" /usr/local/bin/nextcloud-ftp-sync.sh
    systemctl restart vsftpd 2>/dev/null || true
    systemctl restart 'nextcloud-ftp-sync@*' 2>/dev/null || true
fi

# --- 8. Reclaim the old copy --------------------------------------------
# Reached only after the copy verified clean and Nextcloud served requests from
# the new location.
#
# A whole drive is not reclaimed. Deleting a verified second copy of the owner's
# library to free a disk they are about to unplug buys nothing and cannot be
# undone; leaving it makes the old drive a cold spare. Only the internal
# directory, whose space is the entire point of moving off it, is emptied.
if [[ -n "$OLD_MOUNT_DEV" ]]; then
    log_info "=== Your files now live on $NEW_PATH ==="
    log_info "$OLD_MOUNT_DEV still holds a complete copy and is no longer in use — you can unplug it."
else
    log_info "Removing the old copy at $SRC"
    find "$SRC" -mindepth 1 -delete
    log_info "=== Your files now live on $NEW_PATH ($(df -h --output=size "$NEW_PATH" | tail -1 | tr -d ' ')) ==="
fi
