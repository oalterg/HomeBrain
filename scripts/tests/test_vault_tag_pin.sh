#!/usr/bin/env bash
#
# Regression tests for the Vaultwarden image pin.
#
# The bug this pins: Bitwarden Android 2026.8.x declares
# SyncResponseJson.Cipher.data as `String?`, while Vaultwarden <= 1.36.0 still
# emitted that key as a JSON *object* — a legacy back-compat blob built in
# Cipher::to_json(). Every cipher-bearing response therefore died in the client
# with
#
#   JsonDecodingException: Expected beginning of the string, but got { at $.data
#
# which took out POST /api/ciphers (create, and so import), PUT /api/ciphers/{id}
# (edit) and GET /api/sync alike. Upstream deleted the key in 1.37.0
# ("Remove old compatibility code", dani-garcia/vaultwarden#7434), so the floor
# below is the fix, not a preference.
#
# Second thing pinned: the tag lives in TWO places — config/versions.json (which
# update.sh reads to decide whether to rewrite the .env pin) and the
# ${VAULTWARDEN_TAG:-<default>} fallback in docker-compose.yml (which is what a
# box with no .env pin actually runs). Bumping one and forgetting the other
# leaves existing installs on the old image with nothing to show for it.
#
# Third: update.sh must *export* the new tag, not merely rewrite .env — Compose
# prefers an inherited environment variable over --env-file, and load_env has
# already exported the old value by then.
#
#   bash scripts/tests/test_vault_tag_pin.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE="$REPO_ROOT/docker-compose.yml"
VERSIONS="$REPO_ROOT/config/versions.json"
UPDATE_SH="$SCRIPT_DIR/../update.sh"

# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

# The release that removed the legacy `data` object from cipher responses.
MIN_VAULT_TAG="1.37.0"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

expect() {  # expect <label> <expected> <actual>
    if [[ "$3" == "$2" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

echo "Vaultwarden pin"

# --- the two pins agree -----------------------------------------------------
compose_tag="$(grep -Eo 'vaultwarden/server:\$\{VAULTWARDEN_TAG:-[0-9]+\.[0-9]+\.[0-9]+\}' "$COMPOSE" \
    | head -n1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+')"
if [[ -n "$compose_tag" ]]; then
    ok "docker-compose.yml declares a VAULTWARDEN_TAG default ($compose_tag)"
else
    bad "docker-compose.yml declares a VAULTWARDEN_TAG default"
fi

if command -v jq >/dev/null 2>&1; then
    versions_tag="$(jq -r '.vaultwarden.tag // empty' "$VERSIONS" 2>/dev/null)"
else
    versions_tag="$(grep -A2 '"vaultwarden"' "$VERSIONS" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
fi
if [[ -n "$versions_tag" ]]; then
    ok "config/versions.json declares vaultwarden.tag ($versions_tag)"
else
    bad "config/versions.json declares vaultwarden.tag"
fi

expect "the two pins agree" "$versions_tag" "$compose_tag"

# --- both are at or past the fix -------------------------------------------
for label in "versions.json:$versions_tag" "docker-compose.yml:$compose_tag"; do
    where="${label%%:*}"; tag="${label#*:}"
    if [[ -z "$tag" ]]; then
        bad "$where pin is >= $MIN_VAULT_TAG (no tag parsed)"
    elif version_lt "$tag" "$MIN_VAULT_TAG"; then
        bad "$where pin $tag is older than $MIN_VAULT_TAG — cipher responses still carry the legacy \`data\` object, which crashes Bitwarden Android"
    else
        ok "$where pin $tag is >= $MIN_VAULT_TAG"
    fi
done

# --- update.sh re-exports the tag it just wrote -----------------------------
if grep -q 'export VAULTWARDEN_TAG=' "$UPDATE_SH"; then
    ok "update.sh exports VAULTWARDEN_TAG after rewriting the .env pin"
else
    bad "update.sh exports VAULTWARDEN_TAG after rewriting the .env pin (Compose prefers the stale inherited value over --env-file)"
fi

# The export has to come after the sed/append, inside the bump branch, or it
# re-exports the value it was trying to replace.
sed_line="$(grep -n 'VAULTWARDEN_TAG=.\{0,4\}\${new_vault_tag}' "$UPDATE_SH" | tail -n1 | cut -d: -f1)"
exp_line="$(grep -n 'export VAULTWARDEN_TAG=' "$UPDATE_SH" | head -n1 | cut -d: -f1)"
if [[ -n "$sed_line" && -n "$exp_line" ]] && (( exp_line > sed_line )); then
    ok "the export follows the .env rewrite (line $exp_line > $sed_line)"
else
    bad "the export follows the .env rewrite (rewrite=${sed_line:-?}, export=${exp_line:-?})"
fi

# --- version_lt sanity, so a broken helper cannot silently pass the above ---
if version_lt "1.36.0" "1.37.0"; then ok "version_lt 1.36.0 < 1.37.0"; else bad "version_lt 1.36.0 < 1.37.0"; fi
if version_lt "1.37.2" "1.37.0"; then bad "version_lt 1.37.2 !< 1.37.0"; else ok "version_lt 1.37.2 !< 1.37.0"; fi
if version_lt "1.37.0" "1.37.0"; then bad "version_lt 1.37.0 !< 1.37.0"; else ok "version_lt 1.37.0 !< 1.37.0"; fi

echo
echo "passed: $pass  failed: $fail"
[[ "$fail" -eq 0 ]]
