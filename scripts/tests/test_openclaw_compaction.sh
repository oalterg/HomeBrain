#!/usr/bin/env bash
#
# Regression tests for utilities.sh:patch_openclaw_config — specifically the
# compaction budget it writes into openclaw.json.
#
# The bugs these pin:
#   * The OpenClaw schema defaults (reserveTokensFloor 20000, compaction
#     timeoutSeconds 180) are sized for hosted providers. Against a local
#     llama-server they turn every long Telegram chat into "⚠️ Auto-compaction
#     could not recover this turn": compaction has to re-prefill the whole
#     transcript, which on .58 takes 95-503 s, and the 180 s safety timeout
#     aborts it every time (observed: durationMs=180043, reason=timeout).
#   * reserveTokensFloor must follow OpenClaw's own context-aware ladder.
#     Flattening it to 35000 would compact the 81920 Qwen slots at 52% of
#     their window; leaving it at 20000 lets a 131072 window overflow between
#     the compaction trigger and the prompt ceiling.
#   * The whole jq program is one shell string. A stray quote does not fail
#     the install loudly — jq exits non-zero, the `&& mv` never runs, and the
#     box silently keeps the previous config. Parsing it is the point.
#
# The two functions are extracted rather than sourced: utilities.sh sets
# `set -euo pipefail` and sources common.sh off $0 at load time, neither of
# which survives being pulled into a test harness.
#
#   bash scripts/tests/test_openclaw_compaction.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UTILITIES="$TEST_DIR/../utilities.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

command -v jq >/dev/null 2>&1 || { echo "jq is required for this test"; exit 1; }

# --- pull the functions under test out of utilities.sh ----------------------

extract_fn() {
    awk -v fn="$1" '
        $0 == fn "() {" { inside = 1 }
        inside          { print }
        inside && $0 == "}" { exit }
    ' "$UTILITIES"
}

for fn in patch_openclaw_config resolve_llama_ctx_size; do
    body="$(extract_fn "$fn")"
    if [[ -z "$body" ]]; then
        echo "could not extract $fn from $UTILITIES"; exit 1
    fi
    eval "$body"
done

# --- stubs ------------------------------------------------------------------

log_info()  { :; }
log_warn()  { :; }
log_error() { :; }
# No browser on a CI runner; the real one returns empty there too, which is
# the branch that leaves .browser.executablePath untouched.
resolve_browser_path() { return 0; }

# patch_openclaw_config reads the real catalog through SCRIPT_DIR, so point it
# at scripts/ — that also pins maxTokens resolution against the shipped
# platform_models.json rather than a fixture that can drift from it.
SCRIPT_DIR="$TEST_DIR/.."
HB_PLATFORM_TAG="x86_64-vulkan"
MASTER_PASSWORD="test-master-password"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# The shape .58 was actually in when it started failing: a config that has
# been through several OpenClaw upgrades and carries no compaction block.
write_fixture() {
    cat > "$1" <<'JSON'
{
  "browser": { "enabled": true, "headless": true },
  "models": {
    "mode": "merge",
    "providers": {
      "llamacpp": {
        "baseUrl": "http://127.0.0.1:8001/v1",
        "models": [ { "id": "stale-model", "name": "stale-model" } ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "llamacpp/stale-model" },
      "workspace": "/home/homebrain/.openclaw/workspace"
    }
  },
  "gateway": { "port": 18789, "mode": "local" }
}
JSON
}

run_patch() {   # run_patch <ctx-arg> <model-id> ; echoes the patched file path
    local ctx="$1" model="$2" cfg="$TMP_ROOT/openclaw.json"
    write_fixture "$cfg"
    patch_openclaw_config "$cfg" "$model" "$ctx" >/dev/null 2>&1
    printf '%s\n' "$cfg"
}

GLIMMER="Muse-Glimmer-30B-UD-Q4_K_XL"
QWEN27="Qwen3.8-27B-IQ4_XS"

echo "== the jq program runs at all =="
cfg="$(run_patch 131072 "$GLIMMER")"
if jq -e . "$cfg" >/dev/null 2>&1; then
    ok "patched config is valid JSON"
else
    bad "patched config is valid JSON" "jq failed to parse the output (or never wrote it)"
fi

echo "== compaction budget at a 131072 window =="
got=$(jq -r '.agents.defaults.compaction.reserveTokensFloor' "$cfg")
if [[ "$got" == "35000" ]]; then
    ok "reserveTokensFloor is 35000 above a 100k window"
else
    bad "reserveTokensFloor is 35000 above a 100k window" "got $got"
fi

got=$(jq -r '.agents.defaults.compaction.timeoutSeconds' "$cfg")
if [[ "$got" == "1200" ]]; then
    ok "compaction timeoutSeconds is 1200, not the unreachable 180 default"
else
    bad "compaction timeoutSeconds is 1200" "got $got"
fi

# The compaction pass is one provider request; if it were allowed to outlive
# the HTTP timeout the provider would cut it off first and the longer budget
# would buy nothing.
provider_timeout=$(jq -r '.models.providers.llamacpp.timeoutSeconds' "$cfg")
compaction_timeout=$(jq -r '.agents.defaults.compaction.timeoutSeconds' "$cfg")
if (( compaction_timeout < provider_timeout )); then
    ok "compaction timeout ($compaction_timeout s) stays under the provider timeout ($provider_timeout s)"
else
    bad "compaction timeout stays under the provider timeout" \
        "compaction=$compaction_timeout provider=$provider_timeout"
fi

got=$(jq -r '.agents.defaults.compaction.notifyUser' "$cfg")
if [[ "$got" == "true" ]]; then
    ok "notifyUser is on so a multi-minute pass is not silent"
else
    bad "notifyUser is on" "got $got"
fi

echo "== thinking level mirrors the model's own reasoning knob =="
# The two layers are separate transports -- llama-server takes the level as a
# chat-template kwarg, OpenClaw sends thinkingDefault as `reasoning_effort` --
# and when they disagree the dashboard reports a level the model is not running.
# Whatever the catalog pins for this model is what OpenClaw must claim.
catalog_level=$(jq -r --arg id "$GLIMMER" \
    '(.models[] | select(.id == $id) | .extra_flags) // ""' \
    "$TEST_DIR/../../config/platform_models.json" \
    | grep -oE '"reasoning_(strength|effort)"[[:space:]]*:[[:space:]]*"[a-z]+"' \
    | grep -oE '"[a-z]+"$' | tr -d '"')
got=$(jq -r '.agents.defaults.thinkingDefault' "$cfg")
if [[ -n "$catalog_level" && "$got" == "$catalog_level" ]]; then
    ok "thinkingDefault \"$got\" matches the catalog kwarg for $GLIMMER"
else
    bad "thinkingDefault matches the catalog kwarg" \
        "catalog=\"$catalog_level\" config=\"$got\""
fi

# A value outside the schema enum fails config validation and stops the
# gateway loading, so the derivation must never emit one.
case "$got" in
    off|minimal|low|medium|high|xhigh|adaptive|max|ultra)
        ok "thinkingDefault \"$got\" is inside the schema enum" ;;
    *)  bad "thinkingDefault is inside the schema enum" "got \"$got\"" ;;
esac

# The 35B-A3B caps thinking with --reasoning-budget and pins no level kwarg.
# It must clear the key rather than inherit the previous model's, or switching
# models leaves OpenClaw claiming a level nothing set.
cfg35="$TEST_DIR/../../config/platform_models.json"
if jq -e '.models[] | select(.id == "Qwen3.6-35B-A3B-UD-Q5_K_XL")' "$cfg35" >/dev/null 2>&1; then
    cfgnb="$TMP_ROOT/nolevel.json"
    write_fixture "$cfgnb"
    jq '.agents.defaults.thinkingDefault = "xhigh"' "$cfgnb" > "$cfgnb.t" && mv "$cfgnb.t" "$cfgnb"
    patch_openclaw_config "$cfgnb" "Qwen3.6-35B-A3B-UD-Q5_K_XL" 81920 >/dev/null 2>&1
    leftover=$(jq -r '.agents.defaults.thinkingDefault // "<removed>"' "$cfgnb")
    if [[ "$leftover" == "<removed>" ]]; then
        ok "a model pinning no level clears thinkingDefault instead of inheriting"
    else
        bad "a model pinning no level clears thinkingDefault" "stale value survived: \"$leftover\""
    fi
fi

echo "== compaction mode is asserted, not left to drift =="
# The bug this pins is not "which mode" but "nobody says": the seed shipped
# safeguard, nothing re-applied it, and upgraded boxes silently ran default.
got=$(jq -r '.agents.defaults.compaction.mode' "$cfg")
if [[ "$got" == "default" || "$got" == "safeguard" ]]; then
    ok "mode is explicitly asserted (got \"$got\")"
else
    bad "mode is explicitly asserted" "got \"$got\" — an unset mode is the drift this exists to stop"
fi

seed_mode=$(jq -r '.agents.defaults.compaction.mode' "$TEST_DIR/../../config/openclaw.json")
if [[ "$seed_mode" == "$got" ]]; then
    ok "seed template agrees with what the patch asserts (\"$seed_mode\")"
else
    bad "seed template agrees with what the patch asserts" \
        "seed=\"$seed_mode\" patched=\"$got\" — fresh installs and upgraded boxes would diverge"
fi

echo "== a single generation still fits one model call =="
# maxTokens is bounded from the top down: a full-depth prefill plus a
# full-length generation must fit the provider HTTP timeout, or a slow call
# becomes a failed turn. Glimmer is the worst case (slowest prefill x biggest
# window): 131072/287.6 + 16384/16.0 = 1480 s against the 1800 s ceiling.
max_tokens=$(jq -r '.models.providers.llamacpp.models[0].maxTokens' "$cfg")
worst_call=$(python3 -c "print(int(131072/287.6 + $max_tokens/16.0))")
if (( worst_call < provider_timeout )); then
    ok "worst-case Glimmer call ${worst_call}s fits the ${provider_timeout}s provider timeout (maxTokens=$max_tokens)"
else
    bad "worst-case call fits the provider timeout" \
        "maxTokens=$max_tokens implies ${worst_call}s vs provider=${provider_timeout}s"
fi

echo "== the timeouts stay ordered =="
# compaction < heartbeat <= provider. Violating the left half is what broke
# .58 once compaction started succeeding: the heartbeat runs in the same
# session as the chat, so it is just as likely to be the turn that compacts,
# and at its 600 s fallback budget it died on the lane deadline mid-compaction
# and took the owner's queued Telegram message with it.
heartbeat_timeout=$(jq -r '.agents.defaults.heartbeat.timeoutSeconds' "$cfg")
if (( compaction_timeout < provider_timeout )) && (( provider_timeout < heartbeat_timeout )); then
    ok "compaction ($compaction_timeout) < provider ($provider_timeout) < heartbeat ($heartbeat_timeout)"
else
    bad "compaction < provider < heartbeat" \
        "compaction=$compaction_timeout provider=$provider_timeout heartbeat=$heartbeat_timeout"
fi

# A heartbeat turn that outlives its own cadence means two heartbeats overlap
# on the same lane, which is how one slow poll starts blocking user messages.
if (( heartbeat_timeout < 3600 )); then
    ok "heartbeat budget ($heartbeat_timeout s) stays under the 1h cadence"
else
    bad "heartbeat budget stays under the 1h cadence" "got $heartbeat_timeout"
fi

# The Telegram ingress handler wraps the WHOLE turn, so it must be the
# outermost budget. It has no openclaw.json path -- it is an env var in the
# systemd drop-in -- which is exactly why it was missed and why a 300 s
# default silently aborted turns (and their compactions) mid-flight.
ingress_ms=$(grep -oE 'OPENCLAW_TELEGRAM_SPOOLED_HANDLER_TIMEOUT_MS=[0-9]+' "$UTILITIES" | head -1 | cut -d= -f2)
if [[ -z "$ingress_ms" ]]; then
    bad "ingress timeout is set in the gateway drop-in" "no OPENCLAW_TELEGRAM_SPOOLED_HANDLER_TIMEOUT_MS found in utilities.sh"
elif (( ingress_ms / 1000 > heartbeat_timeout )); then
    ok "telegram ingress ($((ingress_ms / 1000)) s) is the outermost budget"
else
    bad "telegram ingress is the outermost budget" \
        "ingress=$((ingress_ms / 1000))s heartbeat=${heartbeat_timeout}s"
fi

# The fallback this replaces is min(600, cadence) — never enough for one
# compaction on a local model. Assert we are not silently back on it.
if (( heartbeat_timeout > 600 )); then
    ok "heartbeat budget is above the 600 s schema fallback"
else
    bad "heartbeat budget is above the 600 s schema fallback" "got $heartbeat_timeout"
fi

echo "== the reserve floor follows the context window =="
cfg81920="$(run_patch 81920 "$QWEN27")"
got=$(jq -r '.agents.defaults.compaction.reserveTokensFloor' "$cfg81920")
if [[ "$got" == "20000" ]]; then
    ok "reserveTokensFloor drops to 20000 at an 81920 window"
else
    bad "reserveTokensFloor drops to 20000 at an 81920 window" "got $got"
fi

got=$(jq -r '.models.providers.llamacpp.models[0].contextWindow' "$cfg81920")
if [[ "$got" == "81920" ]]; then
    ok "contextWindow tracks the ctx_size it was handed"
else
    bad "contextWindow tracks the ctx_size it was handed" "got $got"
fi

echo "== an empty ctx_size still lands on a coherent pair =="
cfgdefault="$(run_patch "" "$GLIMMER")"
win=$(jq -r '.models.providers.llamacpp.models[0].contextWindow' "$cfgdefault")
floor=$(jq -r '.agents.defaults.compaction.reserveTokensFloor' "$cfgdefault")
if [[ "$win" == "131072" && "$floor" == "35000" ]]; then
    ok "empty ctx_size falls back to 131072 with the matching 35000 floor"
else
    bad "empty ctx_size falls back to 131072 with the matching 35000 floor" \
        "contextWindow=$win reserveTokensFloor=$floor"
fi

echo "== idempotency =="
# The dashboard re-patches on every update. A second pass that changes the
# file would restart the gateway each time (refresh_openclaw diffs the file).
cfg2="$TMP_ROOT/idem.json"
write_fixture "$cfg2"
patch_openclaw_config "$cfg2" "$GLIMMER" 131072 >/dev/null 2>&1
cp "$cfg2" "$TMP_ROOT/idem.first"
patch_openclaw_config "$cfg2" "$GLIMMER" 131072 >/dev/null 2>&1
if cmp -s "$TMP_ROOT/idem.first" "$cfg2"; then
    ok "a second patch pass is byte-identical"
else
    bad "a second patch pass is byte-identical" "$(diff "$TMP_ROOT/idem.first" "$cfg2" | head -5)"
fi

echo "== an operator's own compaction tuning survives =="
# We assert three keys; anything else the owner set by hand must be left alone.
cfg3="$TMP_ROOT/operator.json"
write_fixture "$cfg3"
jq '.agents.defaults.compaction = {"keepRecentTokens": 12000, "timeoutSeconds": 300}' \
    "$cfg3" > "$cfg3.tmp" && mv "$cfg3.tmp" "$cfg3"
patch_openclaw_config "$cfg3" "$GLIMMER" 131072 >/dev/null 2>&1
keep=$(jq -r '.agents.defaults.compaction.keepRecentTokens' "$cfg3")
tmo=$(jq -r '.agents.defaults.compaction.timeoutSeconds' "$cfg3")
if [[ "$keep" == "12000" && "$tmo" == "1200" ]]; then
    ok "unrelated compaction keys are preserved, ours are re-asserted"
else
    bad "unrelated compaction keys are preserved, ours are re-asserted" \
        "keepRecentTokens=$keep timeoutSeconds=$tmo"
fi

echo "== resolve_llama_ctx_size is safe when llama-server is not installed =="
# It runs under `set -e` inside a command substitution on the refresh path;
# a non-zero return there would abort the whole openclaw refresh.
if out=$( set -euo pipefail; resolve_llama_ctx_size ); then
    if [[ -z "$out" || "$out" =~ ^[0-9]+$ ]]; then
        ok "returns 0 and echoes either nothing or a bare integer"
    else
        bad "returns 0 and echoes either nothing or a bare integer" "got '$out'"
    fi
else
    bad "returns 0 and echoes either nothing or a bare integer" "non-zero exit under set -e"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
