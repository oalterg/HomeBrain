#!/usr/bin/env bash
#
# Shape tests for F1–F5 of docs/plans/HOUSEHOLD_ACCOUNTS.md §6.2–6.3:
# a vault dump that fails or lands empty aborts the backup; the cleanup
# trap restarts Vaultwarden; restore waits for it and treats import
# failure as fatal; data_only's comment admits the vault rides along.
#
# No live vault, no Docker. Grep/assert against backup.sh and restore.sh
# the way test_backup_retention.sh pins the publish-by-rename contract.
#
#   bash scripts/tests/test_backup_vault_fatal.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BSH="$SCRIPT_DIR/../backup.sh"
RSH="$SCRIPT_DIR/../restore.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

# Scope dump/rsync assertions to the vault block so a Nextcloud `die`
# cannot satisfy them. cleanup() is its own range so the happy-path
# `docker start "$VAULT_CID"` after a successful dump does not count
# as the trap.
vault_block=$(sed -n '/Vault (Vaultwarden)/,/Restarting Vaultwarden/p' "$BSH")
cleanup_block=$(sed -n '/^cleanup() {/,/^}/p' "$BSH")

echo "== cleanup trap restarts the vault container =="
if printf '%s\n' "$cleanup_block" | grep -q 'docker start "\$VAULT_CID"'; then
    ok "cleanup starts VAULT_CID"
else
    bad "cleanup starts VAULT_CID (a signal during the dump would leave vaultwarden stopped)"
fi

echo "== vault dump failures are fatal =="
if printf '%s\n' "$vault_block" | grep -A6 'mysqldump' | grep -q 'die '; then
    ok "failed vault dump uses die"
else
    bad "failed vault dump uses die (still a warning, or the dump line is gone)"
fi
if printf '%s\n' "$vault_block" | grep -A6 'mysqldump' | grep -q 'log_warn'; then
    bad "failed vault dump does not log_warn (it still does)"
else
    ok "failed vault dump does not log_warn"
fi
if printf '%s\n' "$vault_block" | grep -A2 '! -s.*vaultwarden.sql' | grep -q 'die '; then
    ok "empty vault dump uses die"
else
    bad "empty vault dump uses die (still a warning, or the empty-file check is gone)"
fi
if printf '%s\n' "$vault_block" | grep -A2 '! -s.*vaultwarden.sql' | grep -q 'log_warn'; then
    bad "empty vault dump does not log_warn (it still does)"
else
    ok "empty vault dump does not log_warn"
fi

echo "== vault data rsync is fatal =="
if printf '%s\n' "$vault_block" | grep -A1 'rsync -a "\$VAULT_DATA"' | grep -q 'die '; then
    ok "vault data rsync uses die"
else
    bad "vault data rsync uses die (still a warning, or the rsync line is gone)"
fi

echo "== data_only comment mentions the vault =="
if grep '^#   data_only' "$BSH" | grep -qi vault; then
    ok "data_only comment mentions vault"
else
    bad "data_only comment mentions vault (still reads as NC+HA only)"
fi

echo "== restore verifies the vault came back =="
if grep -q 'wait_for_healthy "vaultwarden"' "$RSH"; then
    ok "restore.sh waits for vaultwarden"
else
    bad "restore.sh waits for vaultwarden (wait_for_healthy call is gone)"
fi

echo "== vault DB import failure is fatal =="
if grep -A2 'vaultwarden.sql' "$RSH" | grep -q '|| die'; then
    ok "vault DB import failure uses die"
else
    bad "vault DB import failure uses die (still a warning, or the import line is gone)"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
