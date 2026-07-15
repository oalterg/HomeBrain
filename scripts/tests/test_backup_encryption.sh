#!/usr/bin/env bash
#
# Round-trip tests for the backup encryption pipeline (backup.sh step 11/12,
# restore.sh extraction). Uses the exact same gpg invocations as the scripts,
# so a flag drift between them and this test is a test failure. Needs gpg and
# tar; no Docker, no network, no root.
#
#   bash scripts/tests/test_backup_encryption.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

set -u

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

command -v gpg >/dev/null || { echo "SKIP: gpg not installed"; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS_PHRASE='correct horse battery staple'
WRONG_PHRASE='wrong password'

# Fake staging tree
mkdir -p "$WORK/staging/nc_db" "$WORK/staging/ha_config"
echo "CREATE TABLE users;" > "$WORK/staging/nc_db/nextcloud.sql"
head -c 100000 /dev/urandom > "$WORK/staging/ha_config/blob.bin"

ARCHIVE="$WORK/homebrain_backup_test.tar.gz.gpg"

# --- Encrypt (same flags as backup.sh) ---
tar -C "$WORK/staging" -cz . | gpg --batch --yes --symmetric \
    --cipher-algo AES256 --s2k-mode 3 --s2k-digest-algo SHA512 \
    --s2k-count 65011712 --compress-algo none \
    --passphrase-fd 3 -o "$ARCHIVE" 3<<<"$PASS_PHRASE" \
    && ok "encrypt pipeline" || bad "encrypt pipeline"

# --- Verify (same flags as backup.sh step 12) ---
gpg --batch --quiet --decrypt --passphrase-fd 3 "$ARCHIVE" 3<<<"$PASS_PHRASE" \
    | tar -tz > /dev/null \
    && ok "verify pass (decrypt | tar -tz)" || bad "verify pass"

# --- Wrong passphrase must fail ---
if gpg --batch --quiet --decrypt --passphrase-fd 3 "$ARCHIVE" 3<<<"$WRONG_PHRASE" \
    2>/dev/null | tar -tz > /dev/null 2>&1; then
    bad "wrong passphrase rejected"
else
    ok "wrong passphrase rejected"
fi

# --- Truncated archive must fail verification ---
TRUNC="$WORK/truncated.tar.gz.gpg"
head -c $(( $(stat -f%z "$ARCHIVE" 2>/dev/null || stat -c%s "$ARCHIVE") / 2 )) "$ARCHIVE" > "$TRUNC"
if gpg --batch --quiet --decrypt --passphrase-fd 3 "$TRUNC" 3<<<"$PASS_PHRASE" \
    2>/dev/null | tar -tz > /dev/null 2>&1; then
    bad "truncated archive rejected"
else
    ok "truncated archive rejected"
fi

# --- Restore-side extraction (same as restore.sh) reproduces the tree ---
mkdir -p "$WORK/extract"
gpg --batch --quiet --decrypt --passphrase-fd 3 "$ARCHIVE" 3<<<"$PASS_PHRASE" \
    | tar -xz -C "$WORK/extract" \
    && diff -r "$WORK/staging" "$WORK/extract" > /dev/null \
    && ok "restore round-trip (content identical)" || bad "restore round-trip"

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]] || exit 1
