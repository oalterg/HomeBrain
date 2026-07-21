#!/usr/bin/env bash
#
# Unit tests for the self-MCP bearer token helpers in common.sh.
#
# The token is a contract between two implementations that must never drift:
# src/integrations.py:_self_token() derives it in Python on every dashboard
# request, and common.sh:derive_self_token() rewrites the cached copy from
# bash during a password rotation or a restore. If they disagree, every
# homebrain-self__* agent tool 401s. So the first test pins one against the
# other rather than against a hardcoded digest.
#
#   bash scripts/tests/test_self_token.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# common.sh reads ENV_FILE/HOMEBRAIN_HOME at call time, so point them at the
# sandbox before sourcing. It also mkdir's /var/log/homebrain and probes the
# GPU at source time; on a dev box that is harmless noise, so silence it.
export ENV_FILE="$TMP_ROOT/.env"
export HOMEBRAIN_HOME="$TMP_ROOT/home"
export SELF_TOKEN_FILE="$TMP_ROOT/home/.openclaw/homebrain.token"
mkdir -p "$TMP_ROOT/home/.openclaw"
# shellcheck source=../common.sh disable=SC1091
source "$SCRIPT_DIR/../common.sh" 2>/dev/null
# common.sh exports its own ENV_FILE/HOMEBRAIN_HOME at source time; re-assert.
ENV_FILE="$TMP_ROOT/.env"
HOMEBRAIN_HOME="$TMP_ROOT/home"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

NONCE="4172e9b7e4be108aba7618d038a59d45"
PASS="napped-plausible-sizzling-breeching-onyx"

echo "== derive_self_token matches the Python derivation =="
py_tok="$(python3 -c '
import hashlib, hmac, sys
print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest())
' "$NONCE" "$PASS")"
sh_tok="$(derive_self_token "$NONCE" "$PASS")"
if [[ -n "$py_tok" && "$sh_tok" == "$py_tok" ]]; then
    ok "bash HMAC == python HMAC"
else
    bad "bash HMAC == python HMAC" "bash=$sh_tok python=$py_tok"
fi

# A password containing shell/regex metacharacters must survive both paths —
# generated recovery passwords are hyphenated and the policy allows @!#%+=:?.
py_tok2="$(python3 -c '
import hashlib, hmac, sys
print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest())
' "$NONCE" 'a@b!c#d%e+f=g:h?i-j.k')"
sh_tok2="$(derive_self_token "$NONCE" 'a@b!c#d%e+f=g:h?i-j.k')"
if [[ "$sh_tok2" == "$py_tok2" ]]; then
    ok "metacharacter-heavy password derives identically"
else
    bad "metacharacter-heavy password derives identically" "bash=$sh_tok2 python=$py_tok2"
fi

if derive_self_token "" "$PASS" >/dev/null 2>&1; then
    bad "empty nonce is rejected" "returned success"
else
    ok "empty nonce is rejected"
fi

echo "== env_value takes the LAST match =="
# This is the shape a real box has: .env.template ships an empty placeholder
# and the dashboard appends the real value later. A first-match read yields
# the empty string and would silently derive a garbage token.
cat > "$ENV_FILE" <<EOF
# --- OpenClaw integrations ---
HOMEBRAIN_SELF_NONCE=
MASTER_PASSWORD='${PASS}'
HOMEBRAIN_SELF_NONCE=${NONCE}
EOF
got="$(env_value HOMEBRAIN_SELF_NONCE)"
[[ "$got" == "$NONCE" ]] && ok "placeholder shadowed by real value" \
    || bad "placeholder shadowed by real value" "got '$got'"

got="$(env_value MASTER_PASSWORD)"
[[ "$got" == "$PASS" ]] && ok "surrounding quotes stripped" \
    || bad "surrounding quotes stripped" "got '$got'"

got="$(env_value NOT_A_REAL_KEY)"
[[ -z "$got" ]] && ok "missing key yields empty" || bad "missing key yields empty" "got '$got'"

echo "== refresh_self_token =="
rm -f "$SELF_TOKEN_FILE"
if refresh_self_token >/dev/null 2>&1 && [[ "$(cat "$SELF_TOKEN_FILE")" == "$py_tok" ]]; then
    ok "writes the .env-derived token"
else
    bad "writes the .env-derived token" "got '$(cat "$SELF_TOKEN_FILE" 2>/dev/null)'"
fi

perms="$(ls -l "$SELF_TOKEN_FILE" | cut -c1-10)"
[[ "$perms" == "-rw-------" ]] && ok "written 0600" || bad "written 0600" "got $perms"

# The rotation script calls us with the NEW password before .env carries it.
new_tok="$(derive_self_token "$NONCE" "brand-new-password")"
if refresh_self_token "brand-new-password" >/dev/null 2>&1 \
        && [[ "$(cat "$SELF_TOKEN_FILE")" == "$new_tok" ]]; then
    ok "password argument overrides .env"
else
    bad "password argument overrides .env" "got '$(cat "$SELF_TOKEN_FILE" 2>/dev/null)'"
fi

# umask must not leak out of the subshell and clamp files the caller writes
# afterwards (a rotation continues on to other steps).
before="$(umask)"
refresh_self_token >/dev/null 2>&1
[[ "$(umask)" == "$before" ]] && ok "umask does not leak to the caller" \
    || bad "umask does not leak to the caller" "$before -> $(umask)"

# A box without OpenClaw has no ~/.openclaw: succeed and do nothing.
mv "$TMP_ROOT/home/.openclaw" "$TMP_ROOT/home/.openclaw-gone"
if refresh_self_token >/dev/null 2>&1; then
    ok "no-OpenClaw box is a no-op, not a failure"
else
    bad "no-OpenClaw box is a no-op, not a failure" "returned non-zero"
fi
mv "$TMP_ROOT/home/.openclaw-gone" "$TMP_ROOT/home/.openclaw"

# Missing nonce must fail loudly rather than write a bogus token.
printf "MASTER_PASSWORD='%s'\n" "$PASS" > "$ENV_FILE"
cp "$SELF_TOKEN_FILE" "$TMP_ROOT/before.tok"
if refresh_self_token >/dev/null 2>&1; then
    bad "missing nonce fails" "returned success"
else
    cmp -s "$SELF_TOKEN_FILE" "$TMP_ROOT/before.tok" \
        && ok "missing nonce fails without clobbering the token" \
        || bad "missing nonce fails without clobbering the token" "token was overwritten"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
