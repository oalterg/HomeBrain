#!/usr/bin/env bash
# The heartbeat's working pad: the monitor scratch of OpenClaw's heartbeat job.
#
#   heartbeat_scratch.sh --json    print the scratch with its revision
#   heartbeat_scratch.sh < text    replace the scratch with stdin
#
# Runs as the OpenClaw user against a running gateway. The agent saves its
# Current work through this at the end of a heartbeat: OpenClaw's own route,
# heartbeat_respond, is only offered when every visible reply has to go
# through the message tool (messages.visibleReplies), which HomeBrain does
# not turn on. utilities.sh:seed_heartbeat_scratch uses it too.
set -euo pipefail

job=$(openclaw cron list --all --json 2>/dev/null \
    | jq -r '[.jobs[]? | select(.declarationKey == "heartbeat:main") | .id][0] // empty')
if [[ -z "$job" ]]; then
    echo "No heartbeat job: is the OpenClaw gateway running?" >&2
    exit 1
fi

if [[ "${1:-}" == "--json" ]]; then
    exec openclaw cron scratch "$job" --json
fi

text=$(cat)
# An empty scratch skips every heartbeat from then on.
if [[ -z "${text//[[:space:]]/}" ]]; then
    echo "Refusing to save an empty heartbeat scratch." >&2
    exit 1
fi
printf '%s\n' "$text" | openclaw cron scratch "$job" --file - >/dev/null
echo "Heartbeat scratch saved."
