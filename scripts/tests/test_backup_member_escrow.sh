#!/usr/bin/env bash
#
# Shape tests for member escrow carriage (HOUSEHOLD_ACCOUNTS.md §5.2, §6.4):
# backup stages json + wrap at 0600; restore re-wraps iff vault DB restored;
# wrap file does not remain on dest.
#
#   bash scripts/tests/test_backup_member_escrow.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BSH="$SCRIPT_DIR/../backup.sh"
RSH="$SCRIPT_DIR/../restore.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

grep -q 'member_escrow.json' "$BSH" && ok "backup stages member_escrow.json" \
    || bad "backup stages member_escrow.json"
grep -q 'member_escrow.wrap' "$BSH" && ok "backup stages member_escrow.wrap" \
    || bad "backup stages member_escrow.wrap"
grep -q 'chmod 600 "$STAGING_DIR/member_escrow.wrap"' "$BSH" \
    && ok "wrap file is 0600 in staging" \
    || bad "wrap file is 0600 in staging"
grep -q 'die "Could not capture member vault escrow' "$BSH" \
    && ok "escrow capture failure aborts the backup" \
    || bad "escrow capture failure aborts the backup"

grep -q 'restore-rewrap' "$RSH" && ok "restore calls restore-rewrap" \
    || bad "restore calls restore-rewrap"
grep -q 'HAS_VAULT_DB.*HAS_ESCROW\|HAS_ESCROW.*HAS_VAULT_DB' "$RSH" \
    && ok "escrow restore is gated on vault DB" \
    || bad "escrow restore is gated on vault DB"
grep -q 'rm -f /var/lib/homebrain/member_escrow.wrap' "$RSH" \
    && ok "restore discards wrap file on dest" \
    || bad "restore discards wrap file on dest"
grep -q 'they are one unit' "$RSH" \
    && ok "escrow without vault DB is refused as a unit" \
    || bad "escrow without vault DB is refused as a unit"
grep -q 'Backup unlock is not enabled on this box' "$RSH" \
    && ok "missing dest RECOVERY_BACKUP_KEY fails loudly" \
    || bad "missing dest RECOVERY_BACKUP_KEY fails loudly"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
