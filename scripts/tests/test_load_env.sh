#!/usr/bin/env bash
#
# load_env must not `source` .env. Unquoted PHC hashes (`$argon2id$…`) and
# scrypt params (`scrypt$n=…`) abort under `set -u`, which is how
# /api/system/config 500'd and the dashboard painted the running OpenClaw
# agent as "not installed".
#
#   bash scripts/tests/test_load_env.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export ENV_FILE="$TMP_ROOT/.env"
export HOMEBRAIN_HOME="$TMP_ROOT/home"
mkdir -p "$HOMEBRAIN_HOME"
# shellcheck source=../common.sh disable=SC1091
source "$SCRIPT_DIR/../common.sh" 2>/dev/null
ENV_FILE="$TMP_ROOT/.env"
HOMEBRAIN_HOME="$TMP_ROOT/home"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

PHC='$argon2id$v=19$m=65540$t=3$p=4$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNo'
PARAMS='scrypt$n=32768$r=8$p=1$dklen=32'
export PHC PARAMS

echo "== unquoted PHC under set -u =="
printf '%s\n' \
    "HAS_GPU=true" \
    "VAULT_ADMIN_TOKEN=${PHC}" \
    "RECOVERY_PARAMS=${PARAMS}" \
    "MASTER_PASSWORD=secret" \
    > "$ENV_FILE"

# Do not re-source common.sh here: it mkdir's /var/log/homebrain, which
# fails on a Mac dev box and `set -e` would abort before load_env runs.
if ( set -euo pipefail
     load_env
     [[ "$VAULT_ADMIN_TOKEN" == "$PHC" ]]
     [[ "$RECOVERY_PARAMS" == "$PARAMS" ]]
     [[ "$MASTER_PASSWORD" == "secret" ]]
   ); then
    ok "unquoted \$argon2id / scrypt\$n survive set -u"
else
    bad "unquoted \$argon2id / scrypt\$n survive set -u" "load_env aborted or truncated the value"
fi

echo "== quoted PHC (what update_env_var writes) =="
printf '%s\n' \
    "HAS_GPU=true" \
    "VAULT_ADMIN_TOKEN='${PHC}'" \
    "RECOVERY_PARAMS='${PARAMS}'" \
    > "$ENV_FILE"

# shellcheck source=../common.sh disable=SC1091
source "$SCRIPT_DIR/../common.sh" 2>/dev/null
ENV_FILE="$TMP_ROOT/.env"
if load_env && [[ "$VAULT_ADMIN_TOKEN" == "$PHC" && "$RECOVERY_PARAMS" == "$PARAMS" ]]; then
    ok "single-quoted PHC strips quotes and keeps the hash"
else
    bad "single-quoted PHC strips quotes and keeps the hash" "got '${VAULT_ADMIN_TOKEN:-<unset>}'"
fi

echo "== apostrophe encoding from update_env_var =="
cat > "$ENV_FILE" << 'E'
HAS_GPU=true
NOTE='it'\''s fine'
E
if load_env && [[ "$NOTE" == "it's fine" ]]; then
    ok "undoes '\\'' encoding"
else
    bad "undoes '\\'' encoding" "got '${NOTE:-<unset>}'"
fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
