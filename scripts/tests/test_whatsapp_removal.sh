#!/usr/bin/env bash
#
# Regression tests for utilities.sh:remove_whatsapp_plugins.
#
# The bug this pins: `openclaw plugins uninstall` prompts for confirmation and,
# with no TTY, refuses —
#
#   Uninstall plugin "whatsapp"? [y/N] Error: plugins uninstall requires
#   confirmation input. Re-run in an interactive TTY or pass --force.
#
# — and then **exits 0**. So the original call, which had no `--force` and
# swallowed output with `|| true`, was a silent no-op on every box since it was
# written. Neither the `|| true` nor a stricter exit-code check would have
# caught it, because the command reports success while doing nothing. Found on
# .58 2026-08-15: the retired channel plugin was still being loaded on every
# gateway start and failing with ERR_INTERNAL_ASSERTION.
#
# `openclaw` is a shell function here — shell functions win over PATH — so the
# refusal can be reproduced exactly without a real install.
#
#   bash scripts/tests/test_whatsapp_removal.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UTILITIES="$TEST_DIR/../utilities.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

# --- extract the function under test ----------------------------------------

body="$(awk '
    $0 == "remove_whatsapp_plugins() {" { inside = 1 }
    inside                              { print }
    inside && $0 == "}"                 { exit }
' "$UTILITIES")"
[[ -n "$body" ]] || { echo "could not extract remove_whatsapp_plugins"; exit 1; }
eval "$body"

# --- harness ----------------------------------------------------------------

log_info() { :; }
log_warn() { WARNED="${WARNED}$1"; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
HOMEBRAIN_HOME="$TMP_ROOT/home"

FORCE_SEEN=""
UNINSTALL_CALLS=""

# The real CLI, faithfully: it removes the directory ONLY when --force is
# present, and exits 0 either way.
openclaw() {
    [[ "$1" == "plugins" && "$2" == "uninstall" ]] || return 0
    local pid="$3"; shift 3
    UNINSTALL_CALLS="${UNINSTALL_CALLS}${pid} "
    local forced=""
    for arg in "$@"; do [[ "$arg" == "--force" ]] && forced=1; done
    if [[ -n "$forced" ]]; then
        FORCE_SEEN="${FORCE_SEEN}${pid} "
        rm -rf "${HOMEBRAIN_HOME}/.openclaw/extensions/${pid}"
    fi
    return 0   # <-- exits 0 even when it refuses. This is the whole bug.
}
run_as_admin() { "$@"; }
command() {
    # `command -v openclaw` must find our shell function.
    if [[ "$1" == "-v" && "$2" == "openclaw" ]]; then echo "openclaw"; return 0; fi
    builtin command "$@"
}

seed_plugins() {
    rm -rf "${HOMEBRAIN_HOME}/.openclaw/extensions"
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw/extensions/whatsapp/dist"
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw/extensions/homebrain-whatsapp-login"
    mkdir -p "${HOMEBRAIN_HOME}/.openclaw/extensions/matrix"
}

EXT="${HOMEBRAIN_HOME}/.openclaw/extensions"

echo "== the retired plugins are actually removed =="
seed_plugins
FORCE_SEEN=""; UNINSTALL_CALLS=""; WARNED=""
remove_whatsapp_plugins
if [[ ! -d "$EXT/whatsapp" && ! -d "$EXT/homebrain-whatsapp-login" ]]; then
    ok "both WhatsApp plugin directories are gone"
else
    bad "both WhatsApp plugin directories are gone" \
        "left: $(ls "$EXT" 2>/dev/null | tr '\n' ' ')"
fi

echo "== --force is passed (without it the CLI refuses and still exits 0) =="
if [[ "$FORCE_SEEN" == *"whatsapp"* && "$FORCE_SEEN" == *"homebrain-whatsapp-login"* ]]; then
    ok "--force passed for both plugin ids"
else
    bad "--force passed for both plugin ids" "saw --force for: ${FORCE_SEEN:-<none>}"
fi

echo "== the plugin ID is used, not the npm package name =="
# `@openclaw/whatsapp` also resolves for uninstall, but it does not name the
# directory, so the post-check could never verify the removal.
if [[ "$UNINSTALL_CALLS" == *"whatsapp "* && "$UNINSTALL_CALLS" != *"@openclaw/whatsapp"* ]]; then
    ok "uninstalls by plugin id 'whatsapp'"
else
    bad "uninstalls by plugin id 'whatsapp'" "called with: $UNINSTALL_CALLS"
fi

echo "== unrelated plugins are left alone =="
if [[ -d "$EXT/matrix" ]]; then
    ok "matrix is untouched"
else
    bad "matrix is untouched" "it was removed"
fi

echo "== idempotent: a second run is a clean no-op =="
UNINSTALL_CALLS=""; WARNED=""
remove_whatsapp_plugins
if [[ -z "$UNINSTALL_CALLS" && -z "$WARNED" ]]; then
    ok "nothing to do, nothing attempted, nothing warned"
else
    bad "second run is a no-op" "calls='$UNINSTALL_CALLS' warned='$WARNED'"
fi

echo "== a survivor is reported, not silently accepted =="
# Simulate the pre-fix CLI: accepts the call, exits 0, removes nothing.
seed_plugins
openclaw() { return 0; }
WARNED=""
remove_whatsapp_plugins
if [[ -n "$WARNED" && -d "$EXT/whatsapp" ]]; then
    ok "a plugin that survives uninstall raises a warning"
else
    bad "a plugin that survives uninstall raises a warning" \
        "warned='${WARNED:-<nothing>}' — this is exactly the silent no-op that shipped"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
