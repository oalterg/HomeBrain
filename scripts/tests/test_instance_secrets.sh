#!/usr/bin/env bash
#
# Regression tests for common.sh:merge_instance_secrets — the portable
# instance-secret merge restore.sh runs before starting any container.
#
# The bug this pins: the merge used `while IFS='=' read -r key value`. Bash
# consumes a *trailing* '=' as a field delimiter, and a Fernet key is base64 of
# 32 bytes, so it always ends in exactly one. Every restore shortened
# HOMEBRAIN_EMAIL_KEY from 44 characters to 43. Fernet() rejects a 43-char key,
# so the block that exists specifically to keep account tokens decryptable
# across a restore was destroying the key instead — silently, because the MCP
# decrypt helpers returned the ciphertext unchanged on failure.
#
# Same convention as test_restore_internal.sh: the logic lives in common.sh so
# it can be exercised with no Docker, no network and no root.
#
#   bash scripts/tests/test_instance_secrets.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
skip=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
skipped() { printf '  SKIP  %s — %s\n' "$1" "$2"; skip=$((skip + 1)); }

# update_env_var's replace path uses GNU `sed -i` with no suffix argument, which
# BSD sed (macOS) rejects. The box is Linux and CI is Linux; a developer running
# this on a Mac gets those cases reported as SKIP, never as a pass.
HAVE_GNU_SED=false
sed --version 2>/dev/null | grep -q GNU && HAVE_GNU_SED=true

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A real Fernet key: url-safe base64 of 32 bytes — 44 chars, one '=' of padding.
FERNET="AwBwQAEuTjD5a1TN9GHMe8tp1lJRzS3K7MV8OzsjKP8="

expect() {  # expect <label> <expected> <actual>
    if [[ "$3" == "$2" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

# merge_instance_secrets writes through update_env_var, which reads $ENV_FILE
# and calls harden_env_file (chown root — not available unprivileged).
harden_env_file() { :; }

echo "== base64 padding survives the merge =="

ENV_FILE="$TMP/a.env"
: > "$ENV_FILE"
printf 'HOMEBRAIN_EMAIL_KEY=%s\nHOMEBRAIN_SELF_NONCE=abc123\n' "$FERNET" > "$TMP/src.env"
merge_instance_secrets "$TMP/src.env" >/dev/null 2>&1

expect "trailing '=' is kept (the regression)" "$FERNET" "$(env_value HOMEBRAIN_EMAIL_KEY)"
expect "key is still 44 chars"                 "44"      "${#FERNET}"
expect "a second secret merges too"            "abc123"  "$(env_value HOMEBRAIN_SELF_NONCE)"

echo "== a key that already lost its padding is repaired on import =="

ENV_FILE="$TMP/trunc.env"
: > "$ENV_FILE"
printf 'HOMEBRAIN_EMAIL_KEY=%s\n' "${FERNET%=}" > "$TMP/trunc.src"
merge_instance_secrets "$TMP/trunc.src" >/dev/null 2>&1
expect "43-char key is re-padded to 44" "$FERNET" "$(env_value HOMEBRAIN_EMAIL_KEY)"
expect "pad_fernet_key is a no-op on a correct key" "$FERNET" "$(pad_fernet_key "$FERNET")"
expect "pad_fernet_key is a no-op on empty"         ""       "$(pad_fernet_key "")"

echo "== an existing value is replaced, not duplicated =="

if [[ "$HAVE_GNU_SED" == true ]]; then
    ENV_FILE="$TMP/b.env"
    printf "HOMEBRAIN_EMAIL_KEY='stale-value'\nMASTER_PASSWORD='keep-me'\n" > "$ENV_FILE"
    merge_instance_secrets "$TMP/src.env" >/dev/null 2>&1

    expect "incoming value wins"          "$FERNET"  "$(env_value HOMEBRAIN_EMAIL_KEY)"
    expect "exactly one line for the key" "1"        "$(grep -c '^HOMEBRAIN_EMAIL_KEY=' "$ENV_FILE")"
    expect "unrelated keys untouched"     "keep-me"  "$(env_value MASTER_PASSWORD)"
else
    skipped "replaces an existing value" "needs GNU sed -i"
fi

echo "== values containing '=' are not truncated anywhere =="

ENV_FILE="$TMP/c.env"
: > "$ENV_FILE"
printf 'K=a=b==\n' > "$TMP/eq.env"
merge_instance_secrets "$TMP/eq.env" >/dev/null 2>&1
expect "splits on the first '=' only" "a=b==" "$(env_value K)"

echo "== malformed input is skipped, not merged =="

ENV_FILE="$TMP/d.env"
: > "$ENV_FILE"
printf '# a comment=notakey\n\nNOEQUALS\nGOOD=yes\n' > "$TMP/junk.env"
merge_instance_secrets "$TMP/junk.env" >/dev/null 2>&1
expect "the valid line lands"  "yes" "$(env_value GOOD)"
expect "comments are skipped"  "0"   "$(grep -c '^# a comment=' "$ENV_FILE")"
expect "bare words are skipped" "0"  "$(grep -c '^NOEQUALS' "$ENV_FILE")"

echo "== a final line with no newline still merges =="

ENV_FILE="$TMP/e.env"
: > "$ENV_FILE"
printf 'LAST=%s' "$FERNET" > "$TMP/nonl.env"   # no trailing \n
merge_instance_secrets "$TMP/nonl.env" >/dev/null 2>&1
expect "unterminated last line is read" "$FERNET" "$(env_value LAST)"

echo "== a missing source file is a no-op, not an error =="

ENV_FILE="$TMP/f.env"
: > "$ENV_FILE"
if merge_instance_secrets "$TMP/does-not-exist.env" >/dev/null 2>&1; then
    ok "returns success when there is nothing to merge"
else
    bad "returns success when there is nothing to merge (non-zero exit)"
fi

printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[[ "$fail" -eq 0 ]]
