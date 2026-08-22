#!/usr/bin/env bash
#
# HBK1 envelope + legacy GPG round-trip. Complements test_backup_encryption.sh
# (which pins the raw gpg flags). Needs gpg, tar, python3, cryptography.
#
#   bash scripts/tests/test_backup_unlock.sh
#
set -u

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

command -v gpg >/dev/null || { echo "SKIP: gpg not installed"; exit 0; }
python3 -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" 2>/dev/null \
    || { echo "SKIP: cryptography not installed"; exit 0; }

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CRYPTO="$ROOT/src/backup_crypto.py"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

MASTER='correct-horse-battery-staple-quux'
PHRASE='wobble tundra deputy chrome amulet salsa'
WRONG='wrong-password-not-it'

mkdir -p "$WORK/staging/nc_db" "$WORK/staging/ha_config"
echo "CREATE TABLE users;" > "$WORK/staging/nc_db/nextcloud.sql"
head -c 100000 /dev/urandom > "$WORK/staging/ha_config/blob.bin"

python3 - <<PY
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"].split(":")[0])
import recovery
rec = recovery.build_recovery_record("$PHRASE", 6, 1700000000)
open("$WORK/rk", "w").write(rec["RECOVERY_BACKUP_KEY"])
open("$WORK/rs", "w").write(rec["RECOVERY_BACKUP_SALT"])
print("keys written")
PY
[[ -s "$WORK/rk" ]] && ok "mint recovery wrap key" || bad "mint recovery wrap key"

printf '%s' "$MASTER" > "$WORK/master"
python3 "$CRYPTO" seal \
    --master-file "$WORK/master" \
    --dek-file "$WORK/dek" \
    --header-file "$WORK/header" \
    --recovery-key-file "$WORK/rk" \
    --recovery-salt-file "$WORK/rs" \
    && ok "seal header" || bad "seal header"

BODY="$WORK/body.gpg"
tar -C "$WORK/staging" -cz . | gpg --batch --yes --symmetric \
    --cipher-algo AES256 --s2k-mode 3 --s2k-digest-algo SHA512 \
    --s2k-count 65011712 --compress-algo none \
    --passphrase-fd 3 -o "$BODY" 3< "$WORK/dek" \
    && ok "gpg body (pinned flags)" || bad "gpg body"

ARCHIVE="$WORK/homebrain_backup_test.tar.gz.gpg"
cat "$WORK/header" "$BODY" > "$ARCHIVE" && ok "assemble HBK1" || bad "assemble HBK1"

FMT=$(python3 "$CRYPTO" inspect --archive "$ARCHIVE" --field format)
[[ "$FMT" == "hbk1" ]] && ok "inspect format=hbk1" || bad "inspect format (got $FMT)"
UNLOCK=$(python3 "$CRYPTO" inspect --archive "$ARCHIVE" --field unlock)
[[ "$UNLOCK" == "master_or_phrase" ]] && ok "inspect unlock=master_or_phrase" || bad "inspect unlock (got $UNLOCK)"

open_with() {
    local secret="$1" dest="$2"
    printf '%s' "$secret" > "$WORK/secret"
    python3 "$CRYPTO" open --archive "$ARCHIVE" --secret-file "$WORK/secret" --dek-file "$WORK/opened" \
        && python3 "$CRYPTO" copy-body --archive "$ARCHIVE" \
            | gpg --batch --quiet --decrypt --passphrase-fd 3 3< "$WORK/opened" \
            | tar -xz -C "$dest"
}

mkdir -p "$WORK/from-master" "$WORK/from-phrase" "$WORK/from-wrong"
if open_with "$MASTER" "$WORK/from-master" && diff -r "$WORK/staging" "$WORK/from-master" >/dev/null; then
    ok "open with master password"
else
    bad "open with master password"
fi
if open_with "$PHRASE" "$WORK/from-phrase" && diff -r "$WORK/staging" "$WORK/from-phrase" >/dev/null; then
    ok "open with recovery phrase"
else
    bad "open with recovery phrase"
fi
if open_with "$WRONG" "$WORK/from-wrong" 2>/dev/null; then
    bad "wrong secret rejected"
else
    ok "wrong secret rejected"
fi

# Truncated body must fail gpg/tar even if the header unwraps.
TRUNC="$WORK/truncated.tar.gz.gpg"
head -c $(( $(wc -c < "$ARCHIVE") / 2 )) "$ARCHIVE" > "$TRUNC"
printf '%s' "$MASTER" > "$WORK/secret"
if python3 "$CRYPTO" open --archive "$TRUNC" --secret-file "$WORK/secret" --dek-file "$WORK/opened-trunc" 2>/dev/null \
    && python3 "$CRYPTO" copy-body --archive "$TRUNC" \
        | gpg --batch --quiet --decrypt --passphrase-fd 3 3< "$WORK/opened-trunc" 2>/dev/null \
        | tar -tz >/dev/null 2>&1; then
    bad "truncated body rejected"
else
    ok "truncated body rejected"
fi

# Legacy file: raw gpg with master, phrase does not decrypt.
LEGACY="$WORK/legacy.tar.gz.gpg"
tar -C "$WORK/staging" -cz . | gpg --batch --yes --symmetric \
    --cipher-algo AES256 --s2k-mode 3 --s2k-digest-algo SHA512 \
    --s2k-count 65011712 --compress-algo none \
    --passphrase-fd 3 -o "$LEGACY" 3<<<"$MASTER" \
    && ok "legacy encrypt" || bad "legacy encrypt"
LFMT=$(python3 "$CRYPTO" inspect --archive "$LEGACY" --field format)
[[ "$LFMT" == "legacy" ]] && ok "inspect format=legacy" || bad "inspect legacy (got $LFMT)"
mkdir -p "$WORK/legacy-out"
if gpg --batch --quiet --decrypt --passphrase-fd 3 "$LEGACY" 3<<<"$MASTER" \
        | tar -xz -C "$WORK/legacy-out" \
        && diff -r "$WORK/staging" "$WORK/legacy-out" >/dev/null; then
    ok "legacy opens with master"
else
    bad "legacy opens with master"
fi
if gpg --batch --quiet --decrypt --passphrase-fd 3 "$LEGACY" 3<<<"$PHRASE" \
        2>/dev/null | tar -tz >/dev/null 2>&1; then
    bad "legacy rejects phrase"
else
    ok "legacy rejects phrase"
fi

echo
echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]] || exit 1
