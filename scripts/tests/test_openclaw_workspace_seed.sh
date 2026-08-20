#!/usr/bin/env bash
#
# Regression tests for utilities.sh:seed_openclaw_workspace and
# heartbeat_checklist_is_empty.
#
#   bash scripts/tests/test_openclaw_workspace_seed.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UTILITIES="$TEST_DIR/../utilities.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

extract_fn() {
    awk -v fn="$1" '
        $0 == fn "() {" { inside = 1 }
        inside          { print }
        inside && $0 == "}" { exit }
    ' "$UTILITIES"
}

for fn in heartbeat_checklist_is_empty seed_openclaw_workspace; do
    body="$(extract_fn "$fn")"
    if [[ -z "$body" ]]; then
        echo "could not extract $fn from $UTILITIES"; exit 1
    fi
    eval "$body"
done

log_info()  { :; }
log_warn()  { :; }
log_error() { :; }

SCRIPT_DIR="$TEST_DIR/.."
HOMEBRAIN_USER="no-such-homebrain-user"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
HOMEBRAIN_HOME="$TMP_ROOT/home"

echo "== empty checklist detection matches OpenClaw's skip rules =="
if heartbeat_checklist_is_empty $'# Heading\n\n- [ ]\n'; then
    ok "headings and empty checkboxes count as empty"
else
    bad "headings and empty checkboxes count as empty" "treated as body"
fi

# OpenClaw 2026.7.1-2 ships a comments-only HEARTBEAT.md that skips the
# hourly turn. We have to classify it empty so an upgrade replaces it.
if heartbeat_checklist_is_empty $'<!-- Heartbeat template; comments-only content prevents scheduled heartbeat API calls. -->\n\n# Keep this file empty (or with only comments) to skip heartbeat API calls.\n\n# Add tasks below when you want the agent to check something periodically.\n'; then
    ok "OpenClaw's stock comments-only HEARTBEAT.md counts as empty"
else
    bad "OpenClaw's stock comments-only HEARTBEAT.md counts as empty" \
        "an upgraded box would keep a file that skips every heartbeat"
fi

if heartbeat_checklist_is_empty $'#TODO ping Oliver about the holiday dates\n'; then
    bad "#TODO without a space is not an ATX heading" \
        "would overwrite current work on the next update"
else
    ok "#TODO without a space is not an ATX heading"
fi

if heartbeat_checklist_is_empty "$(cat "$TEST_DIR/../../config/openclaw-workspace/HEARTBEAT.md")"; then
    bad "the seeded HEARTBEAT.md is not empty" "empty-checklist detector would skip hourly heartbeats"
else
    ok "the seeded HEARTBEAT.md is not empty"
fi

echo "== seed creates missing files and leaves filled ones =="
mkdir -p "$HOMEBRAIN_HOME/.openclaw"
seed_openclaw_workspace
ws="$HOMEBRAIN_HOME/.openclaw/workspace"
for f in HEARTBEAT.md MEMORY.md; do
    if [[ -f "$ws/$f" ]]; then
        ok "seeded $f"
    else
        bad "seeded $f" "missing $ws/$f"
    fi
done
if [[ ! -f "$ws/AGENTS.md" ]]; then
    ok "did not stub AGENTS.md before OpenClaw can write its default"
else
    bad "did not stub AGENTS.md before OpenClaw can write its default" \
        "would block the OpenClaw bootstrap file"
fi
if [[ -d "$ws/memory" ]]; then
    ok "created memory/"
else
    bad "created memory/" "missing $ws/memory"
fi

printf '\nOliver prefers tea.\n' >> "$ws/HEARTBEAT.md"
printf '\nDo not overwrite me.\n' >> "$ws/MEMORY.md"
seed_openclaw_workspace
if grep -q 'Oliver prefers tea' "$ws/HEARTBEAT.md"; then
    ok "a filled HEARTBEAT.md is not replaced"
else
    bad "a filled HEARTBEAT.md is not replaced" "seed overwrote current work"
fi
if grep -q 'Do not overwrite me' "$ws/MEMORY.md"; then
    ok "an existing MEMORY.md is not replaced"
else
    bad "an existing MEMORY.md is not replaced" "seed overwrote durable facts"
fi

echo "== empty HEARTBEAT.md is re-seeded =="
printf '# Heartbeat\n\n- [ ]\n' > "$ws/HEARTBEAT.md"
seed_openclaw_workspace
if grep -q 'Current work' "$ws/HEARTBEAT.md"; then
    ok "headings-only HEARTBEAT.md is replaced with the template"
else
    bad "headings-only HEARTBEAT.md is replaced with the template" \
        "left a file that would skip heartbeats"
fi

echo "== AGENTS.md gets the HomeBrain block once =="
seed_openclaw_workspace 1
if [[ -f "$ws/AGENTS.md" ]] && grep -q '## HomeBrain memory' "$ws/AGENTS.md"; then
    ok "creates AGENTS.md after OpenClaw would have started, if still missing"
else
    bad "creates AGENTS.md after OpenClaw would have started, if still missing" \
        "missing $ws/AGENTS.md"
fi
printf '# Agent\n\nBe brief.\n' > "$ws/AGENTS.md"
seed_openclaw_workspace 1
if grep -q '## HomeBrain memory' "$ws/AGENTS.md" && grep -q 'Be brief' "$ws/AGENTS.md"; then
    ok "appends the memory block without dropping existing instructions"
else
    bad "appends the memory block without dropping existing instructions" \
        "$(head -5 "$ws/AGENTS.md")"
fi
cp "$ws/AGENTS.md" "$TMP_ROOT/agents.first"
seed_openclaw_workspace 1
if cmp -s "$TMP_ROOT/agents.first" "$ws/AGENTS.md"; then
    ok "a second seed does not duplicate the memory block"
else
    bad "a second seed does not duplicate the memory block" \
        "AGENTS.md changed on the second pass"
fi

echo "== missing templates are a skip, not an abort =="
# production utilities.sh is set -e; a bare cp of a missing HEARTBEAT.md
# would take down refresh_openclaw. Empty template dir must return.
empty_root="$TMP_ROOT/nosrc"
mkdir -p "$empty_root/scripts" "$empty_root/config/openclaw-workspace"
if (
    set -euo pipefail
    SCRIPT_DIR="$empty_root/scripts"
    seed_openclaw_workspace 1
); then
    ok "empty template dir does not abort under set -e"
else
    bad "empty template dir does not abort under set -e" "seed exited non-zero"
fi
if grep -q 'Current work' "$ws/HEARTBEAT.md"; then
    ok "a skipped seed leaves the existing HEARTBEAT.md alone"
else
    bad "a skipped seed leaves the existing HEARTBEAT.md alone" \
        "existing workspace file was lost"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
