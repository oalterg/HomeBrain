#!/usr/bin/env bash
#
# Pre-flight guards for move_nc_data.sh.
#
# This script's second act is `wipefs -a` followed by `mkfs.ext4`. Everything
# that decides whether it gets that far is worth pinning: pointed at the wrong
# device it destroys the root filesystem, the backup archives, or — by running
# out of room halfway — the copy it was making.
#
# Real loop devices rather than fakes, because the checks read real block-device
# geometry (blockdev, findmnt, lsblk). Needs root for losetup and mount.
#
#   sudo bash scripts/tests/test_move_nc_data.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOVE="$SCRIPT_DIR/../move_nc_data.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

if [[ $EUID -ne 0 ]]; then
    echo "SKIP: needs root (losetup, mount)." >&2
    exit 0
fi

TMP="$(mktemp -d)"
LOOPS=()
cleanup() {
    umount "$TMP/backupmnt" 2>/dev/null
    umount "$TMP/livemnt" 2>/dev/null
    umount "$TMP/foreignmnt" 2>/dev/null
    # The foreign-drive guard mounts the candidate to inspect it and unmounts
    # before refusing; the directory it used is this script's to clear up.
    umount /mnt/nextcloud-data.new 2>/dev/null
    rmdir /mnt/nextcloud-data.new 2>/dev/null
    for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null; done
    rm -rf "$TMP"
}
trap cleanup EXIT

mkloop() {  # mkloop <megabytes> -> prints /dev/loopN
    local mb="$1" img
    img="$TMP/img$RANDOM.raw"
    truncate -s "${mb}M" "$img"
    local dev
    dev="$(losetup -f --show "$img")" || return 1
    LOOPS+=("$dev")
    echo "$dev"
}

# A fake install root: .env is all move_nc_data.sh reads out of it.
mkdir -p "$TMP/install" "$TMP/ncdata" "$TMP/backupmnt"
head -c 20000000 /dev/zero > "$TMP/ncdata/bulk"   # ~20 MB of "user files"
cat > "$TMP/install/.env" <<EOF
NEXTCLOUD_DATA_DIR=$TMP/ncdata
BACKUP_MOUNTDIR=$TMP/backupmnt
HAS_GPU=false
EOF

# Every case must fail, and fail for the stated reason — an exit code alone
# would also be satisfied by the script dying on something unrelated.
refuses() {  # refuses <description> <expected message fragment> <args...>
    local desc="$1" want="$2"; shift 2
    local out rc
    out="$(INSTALL_DIR="$TMP/install" bash "$MOVE" "$@" 2>&1)"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        bad "$desc (script SUCCEEDED — it should have refused)"
    elif [[ "$out" != *"$want"* ]]; then
        bad "$desc (refused, but not for the right reason: ${out##*$'\n'})"
    else
        ok "$desc"
    fi
}

echo "== refuses to touch a device it must not format =="

root_src="$(findmnt -n -o SOURCE / | sed 's/\[.*\]//')"
if [[ -b "$root_src" ]]; then
    refuses "refuses the root filesystem" "root filesystem" "$root_src"
else
    echo "  skip  root filesystem ($root_src is not a block device here)"
fi

backup_dev="$(mkloop 32)"
mkfs.ext4 -q -F "$backup_dev"
mount "$backup_dev" "$TMP/backupmnt"
refuses "refuses the backup drive" "backup drive" "$backup_dev"

echo "== refuses a move that cannot succeed =="

small="$(mkloop 8)"    # 8 MB for ~20 MB of files
refuses "refuses a drive smaller than the data" "plus overhead" "$small"

refuses "refuses a path that is not a block device" "not a block device" "$TMP/nosuchdev"
refuses "refuses no argument at all" "Usage" ""

refuses "refuses --internal when the files are already internal" \
    "already on the internal disk" "--internal"

echo "== refuses to move a drive onto itself =="

# The files live on a drive, and that same drive is offered as the target.
# Only reachable since the move stopped being one-way.
livedev="$(mkloop 64)"
mkfs.ext4 -q -F -L NextcloudData "$livedev"
mkdir -p "$TMP/livemnt"
mount "$livedev" "$TMP/livemnt"
head -c 1000000 /dev/zero > "$TMP/livemnt/bulk"
cat > "$TMP/install/.env" <<EOF
NEXTCLOUD_DATA_DIR=$TMP/livemnt
BACKUP_MOUNTDIR=$TMP/backupmnt
HAS_GPU=false
EOF
refuses "refuses moving a drive onto itself" "already on" "$livedev"

echo "== refuses a drive carrying another box's library =="

# A files drive moved from a dead box to a replacement one: same NextcloudData
# label, full of somebody's photos, and this box has never written to it. The
# label alone used to mean "my own half-finished copy — resume", and pass 1
# runs with --delete.
foreign="$(mkloop 64)"
mkfs.ext4 -q -F -L NextcloudData "$foreign"
mkdir -p "$TMP/foreignmnt"
mount "$foreign" "$TMP/foreignmnt"
mkdir -p "$TMP/foreignmnt/alice/files"
echo "the only copy of a wedding photo" > "$TMP/foreignmnt/alice/files/photo.jpg"
touch "$TMP/foreignmnt/.ncdata"
umount "$TMP/foreignmnt"
cat > "$TMP/install/.env" <<EOF
NEXTCLOUD_DATA_DIR=$TMP/ncdata
BACKUP_MOUNTDIR=$TMP/backupmnt
HAS_GPU=false
EOF
refuses "refuses a NextcloudData drive this box did not write" "did not put it there" "$foreign"

# Refusing is only half of it: the files have to still be there afterwards.
mount "$foreign" "$TMP/foreignmnt"
if [[ -s "$TMP/foreignmnt/alice/files/photo.jpg" ]]; then
    ok "the refused drive still holds its files"
else
    bad "the refused drive still holds its files"
fi
umount "$TMP/foreignmnt"

echo
echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
