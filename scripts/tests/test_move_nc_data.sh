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
refuses "refuses a drive smaller than the data" "needs" "$small"

refuses "refuses a path that is not a block device" "not a block device" "$TMP/nosuchdev"
refuses "refuses no argument at all" "Usage" ""

cat > "$TMP/install/.env" <<EOF
NEXTCLOUD_DATA_DIR=/mnt/nextcloud-data
BACKUP_MOUNTDIR=$TMP/backupmnt
HAS_GPU=false
EOF
big="$(mkloop 64)"
refuses "refuses when the data is already on the target" "already on" "$big"

echo
echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
