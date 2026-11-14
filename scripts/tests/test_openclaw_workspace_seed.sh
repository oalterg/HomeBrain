#!/usr/bin/env bash
#
# Regression tests for utilities.sh:seed_openclaw_workspace,
# seed_heartbeat_scratch and heartbeat_checklist_is_empty.
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

for fn in heartbeat_checklist_is_empty seed_heartbeat_scratch seed_openclaw_workspace; do
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

# OpenClaw 2026.7.1-2 shipped a comments-only HEARTBEAT.md that skips the
# hourly turn, and doctor imports it into the scratch as it is. We have to
# classify it empty so an upgrade replaces it.
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

if heartbeat_checklist_is_empty "$(cat "$TEST_DIR/../../config/openclaw-workspace/heartbeat.scratch")"; then
    bad "the seeded heartbeat scratch is not empty" "empty-checklist detector would skip hourly heartbeats"
else
    ok "the seeded heartbeat scratch is not empty"
fi

echo "== heartbeat scratch is seeded through the gateway, never over work =="
# seed_heartbeat_scratch reaches the gateway through heartbeat_scratch.sh
# behind run_as_admin. SCRATCH is what its --json read answers ("fail" = no
# gateway or no heartbeat job); a write lands in $TMP_ROOT/written.
openclaw() { :; }
run_as_admin() {
    case "$*" in
        "bash $SCRIPT_DIR/heartbeat_scratch.sh --json")
            [[ "$SCRATCH" == fail ]] && return 1
            printf '%s' "$SCRATCH" ;;
        "bash $SCRIPT_DIR/heartbeat_scratch.sh") cat > "$TMP_ROOT/written" ;;
        *) echo "unexpected: $*" >> "$TMP_ROOT/unexpected" ;;
    esac
}
scratch_case() {   # scratch_case <name> <want: seeded|kept> — uses SCRATCH
    rm -f "$TMP_ROOT/written"
    seed_heartbeat_scratch
    local got=kept
    [[ -f "$TMP_ROOT/written" ]] && got=seeded
    if [[ "$got" == "$2" ]]; then ok "$1"; else bad "$1" "scratch was $got"; fi
}
SCRATCH='{"scratch":null,"currentRevision":0,"maxBytes":262144}'
scratch_case "a missing scratch is seeded" seeded
if cmp -s "$TMP_ROOT/written" "$TEST_DIR/../../config/openclaw-workspace/heartbeat.scratch"; then
    ok "the seed is the template, byte for byte"
else
    bad "the seed is the template, byte for byte" "$(head -3 "$TMP_ROOT/written" 2>/dev/null)"
fi
SCRATCH='{"scratch":{"content":"# Heartbeat\n\n- [ ]\n","revision":3},"currentRevision":3,"maxBytes":262144}'
scratch_case "a headings-only scratch is replaced with the template" seeded
SCRATCH='{"scratch":{"content":"## Current work\nOliver prefers tea.\n","revision":3},"currentRevision":3,"maxBytes":262144}'
scratch_case "a filled scratch is not replaced" kept
SCRATCH=fail
scratch_case "a scratch that could not be read is not replaced" kept
SCRATCH=''
scratch_case "an empty answer is not taken for an empty scratch" kept
if [[ -f "$TMP_ROOT/unexpected" ]]; then
    bad "nothing else is run" "$(cat "$TMP_ROOT/unexpected")"
else
    ok "nothing else is run"
fi

echo "== heartbeat_scratch.sh finds the heartbeat job and guards the write =="
# A fake `openclaw` on PATH: `cron list` answers $JOBS, `cron scratch` logs
# its arguments and stdin.
FAKE_BIN="$TMP_ROOT/bin"; mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/openclaw" <<'FAKE'
#!/usr/bin/env bash
case "$1 $2" in
    "cron list") printf '%s' "$JOBS" ;;
    "cron scratch")
        echo "$*" >> "$FAKE_LOG"
        if [[ "$4" == "--json" ]]; then echo '{"scratch":null,"currentRevision":0,"maxBytes":262144}'
        else cat > "$FAKE_LOG.stdin"; fi ;;
esac
FAKE
chmod +x "$FAKE_BIN/openclaw"
helper() { PATH="$FAKE_BIN:$PATH" FAKE_LOG="$TMP_ROOT/fake.log" JOBS="$JOBS" bash "$TEST_DIR/../heartbeat_scratch.sh" "$@"; }
JOBS='{"jobs":[{"id":"job-0","declarationKey":"skill-collection-review:main"},{"id":"job-1","declarationKey":"heartbeat:main"}]}'
out=$(printf 'Checklist\n\n## Current work\nwater the plants\n' | helper 2>&1); rc=$?
if [[ $rc -eq 0 && "$(cat "$TMP_ROOT/fake.log")" == "cron scratch job-1 --file -" ]] \
        && grep -q 'water the plants' "$TMP_ROOT/fake.log.stdin"; then
    ok "stdin replaces the heartbeat job's scratch"
else
    bad "stdin replaces the heartbeat job's scratch" "rc=$rc out=$out log=$(cat "$TMP_ROOT/fake.log" 2>/dev/null)"
fi
rm -f "$TMP_ROOT/fake.log"
if helper --json | jq -e 'has("maxBytes")' >/dev/null && [[ "$(cat "$TMP_ROOT/fake.log")" == "cron scratch job-1 --json" ]]; then
    ok "--json reads it"
else
    bad "--json reads it" "log=$(cat "$TMP_ROOT/fake.log" 2>/dev/null)"
fi
rm -f "$TMP_ROOT/fake.log"
if printf '\n  \n' | helper >/dev/null 2>&1 || [[ -f "$TMP_ROOT/fake.log" ]]; then
    bad "an empty scratch is refused" "it would skip every heartbeat from then on"
else
    ok "an empty scratch is refused"
fi
for JOBS in '{"jobs":[{"id":"job-0","declarationKey":"skill-collection-review:main"}]}' ''; do
    if echo text | helper >/dev/null 2>&1 || [[ -f "$TMP_ROOT/fake.log" ]]; then
        bad "no heartbeat job, no write (jobs='${JOBS:0:20}')" "wrote anyway"
    else
        ok "no heartbeat job, no write (jobs='${JOBS:0:20}')"
    fi
done

echo "== seed creates missing files and leaves filled ones =="
mkdir -p "$HOMEBRAIN_HOME/.openclaw"
seed_openclaw_workspace
ws="$HOMEBRAIN_HOME/.openclaw/workspace"
for f in MEMORY.md; do
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

if [[ ! -e "$ws/HEARTBEAT.md" ]]; then
    ok "does not write the retired HEARTBEAT.md"
else
    bad "does not write the retired HEARTBEAT.md" "doctor would import it over the scratch on every upgrade"
fi
printf '\nDo not overwrite me.\n' >> "$ws/MEMORY.md"
seed_openclaw_workspace
if grep -q 'Do not overwrite me' "$ws/MEMORY.md"; then
    ok "an existing MEMORY.md is not replaced"
else
    bad "an existing MEMORY.md is not replaced" "seed overwrote durable facts"
fi

echo "== AGENTS.md gets the HomeBrain block once =="
seed_openclaw_workspace 1
if [[ -f "$ws/AGENTS.md" ]] && grep -q '## HomeBrain memory' "$ws/AGENTS.md"; then
    ok "creates AGENTS.md after OpenClaw would have started, if still missing"
else
    bad "creates AGENTS.md after OpenClaw would have started, if still missing" \
        "missing $ws/AGENTS.md"
fi
if grep -q '## HomeBrain identity' "$ws/AGENTS.md"; then
    ok "seeded AGENTS.md includes the identity block"
else
    bad "seeded AGENTS.md includes the identity block" \
        "missing ## HomeBrain identity"
fi
if grep -q '## HomeBrain setup' "$ws/AGENTS.md"; then
    ok "seeded AGENTS.md includes the setup block"
else
    bad "seeded AGENTS.md includes the setup block" \
        "missing ## HomeBrain setup"
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

echo "== AGENTS.md identity block on upgrade =="
printf '# Agent\n\n## HomeBrain memory\n\n- Durable facts.\n' > "$ws/AGENTS.md"
seed_openclaw_workspace 1
if grep -q '## HomeBrain identity' "$ws/AGENTS.md" \
        && grep -q 'Durable facts' "$ws/AGENTS.md" \
        && grep -q '## HomeBrain memory' "$ws/AGENTS.md"; then
    ok "appends the identity block without dropping the memory block"
else
    bad "appends the identity block without dropping the memory block" \
        "$(cat "$ws/AGENTS.md")"
fi
cp "$ws/AGENTS.md" "$TMP_ROOT/agents.identity"
seed_openclaw_workspace 1
if cmp -s "$TMP_ROOT/agents.identity" "$ws/AGENTS.md"; then
    ok "a second seed does not duplicate the identity block"
else
    bad "a second seed does not duplicate the identity block" \
        "AGENTS.md changed on the second pass"
fi

echo "== AGENTS.md setup block on upgrade =="
printf '# Agent\n\n## HomeBrain memory\n\n- Durable facts.\n\n## HomeBrain identity\n\n- You are the clerk.\n' > "$ws/AGENTS.md"
seed_openclaw_workspace 1
if grep -q '## HomeBrain setup' "$ws/AGENTS.md" \
        && grep -q 'You are the clerk' "$ws/AGENTS.md" \
        && grep -q 'Durable facts' "$ws/AGENTS.md"; then
    ok "appends the setup block without dropping identity"
else
    bad "appends the setup block without dropping identity" \
        "$(cat "$ws/AGENTS.md")"
fi
cp "$ws/AGENTS.md" "$TMP_ROOT/agents.setup"
seed_openclaw_workspace 1
if cmp -s "$TMP_ROOT/agents.setup" "$ws/AGENTS.md"; then
    ok "a second seed does not duplicate the setup block"
else
    bad "a second seed does not duplicate the setup block" \
        "AGENTS.md changed on the second pass"
fi

echo "== missing templates are a skip, not an abort =="
# production utilities.sh is set -e; a bare cp of a missing template
# would take down refresh_openclaw. Empty template dir must return.
empty_root="$TMP_ROOT/nosrc"
mkdir -p "$empty_root/scripts" "$empty_root/config/openclaw-workspace"
if (
    set -euo pipefail
    SCRIPT_DIR="$empty_root/scripts"
    seed_openclaw_workspace 1
    seed_heartbeat_scratch
); then
    ok "empty template dir does not abort under set -e"
else
    bad "empty template dir does not abort under set -e" "seed exited non-zero"
fi
if grep -q 'Do not overwrite me' "$ws/MEMORY.md"; then
    ok "a skipped seed leaves the existing MEMORY.md alone"
else
    bad "a skipped seed leaves the existing MEMORY.md alone" \
        "existing workspace file was lost"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
