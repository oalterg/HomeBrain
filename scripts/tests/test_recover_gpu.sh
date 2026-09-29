#!/usr/bin/env bash
#
# Decision logic for scripts/recover_gpu.sh. The hardware rebind is not
# exercised here; these cases pin when the timer must leave the GPU alone.
#
#   bash scripts/tests/test_recover_gpu.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../recover_gpu.sh disable=SC1091
source "$SCRIPT_DIR/../recover_gpu.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        got %s\n' "$1" "$2"; fail=$((fail + 1)); }

expect() {
    local label="$1" want="$2"
    shift 2
    local got
    got=$(recover_action "$@")
    if [[ "$got" == "$want" ]]; then
        ok "$label"
    else
        bad "$label" "$got (wanted $want)"
    fi
}

echo "== recover_action =="
expect "running chat is left alone" skip-up active success 0 0 0
expect "model load is left alone" skip-up activating success 0 0 0
expect "clean stop is left alone" skip-stopped inactive success 0 0 1
expect "media job owns the GPU" skip-media failed exit-code 1 0 1
expect "cooldown blocks a second rebind" skip-cooldown failed exit-code 0 1 1
expect "no SYCL device rebinds the Arc" rebind failed exit-code 0 0 1
expect "other start-limit failure just restarts" restart failed exit-code 0 0 0
expect "unresponsive active process restarts" restart active success 0 0 0 1
expect "paused media beats unresponsive health" skip-media active success 1 0 0 1
expect "partial rebind is retried after llama was stopped" rebind inactive success 0 0 1 0 1

echo "== log_says_no_device =="
if log_says_no_device "No device of requested type available. Please check"; then
    ok "sycl no-device line"
else
    bad "sycl no-device line" "missed"
fi
if log_says_no_device "Main process exited, code=dumped, status=6/ABRT"; then
    bad "abort alone is not a missing device" "matched"
else
    ok "abort alone is not a missing device"
fi

echo "== recovery failure paths =="
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
SYSFS="$TMP/sys"
DRI="$TMP/dri"
PENDING="$TMP/pending"
CALLS="$TMP/calls"
mkdir -p "$SYSFS/bus/pci/drivers/xe" "$SYSFS/bus/pci/devices/0000:07:00.0/drm/renderD129" "$DRI"
echo 0x8086 > "$SYSFS/bus/pci/devices/0000:07:00.0/vendor"
ln -s "$SYSFS/bus/pci/devices/0000:07:00.0" "$SYSFS/bus/pci/drivers/xe/0000:07:00.0"
ln -s "$SYSFS/bus/pci/drivers/xe" "$SYSFS/bus/pci/devices/0000:07:00.0/driver"
touch "$DRI/renderD129"
say() { echo "$*" >> "$CALLS"; }
sleep() { :; }
media_busy() { return 1; }
systemctl() {
    echo "$*" >> "$CALLS"
    case "$*" in
        'is-active --quiet whisper-server'|'is-active --quiet whisper-proxy') return 0 ;;
    esac
}
fuser() { echo "fuser $*" >> "$CALLS"; return 1; }
timeout() {
    echo "timeout $*" >> "$CALLS"
    case "$*" in
        *'/unbind'*)
            rm -f "$SYSFS/bus/pci/drivers/xe/0000:07:00.0" "$SYSFS/bus/pci/devices/0000:07:00.0/driver" ;;
        *'/bind'*) return 1 ;;
    esac
}
rebind_xe >/dev/null 2>&1 || true
if grep -q '^start whisper-proxy$' "$CALLS"; then
    ok "proxy restored after failed bind"
else
    bad "proxy restored after failed bind" "proxy was never started"
fi
if grep -q "fuser .*${DRI}/renderD129" "$CALLS" && ! grep -q 'renderD128' "$CALLS"; then
    ok "busy check follows the selected PCI device"
else
    bad "busy check follows the selected PCI device" "wrong render node checked"
fi
if [[ -f "$PENDING" ]] && [[ "$(xe_bdf)" == 0000:07:00.0 ]]; then
    ok "unbound PCI target survives a failed bind"
else
    bad "unbound PCI target survives a failed bind" "target lost"
fi
: > "$CALLS"
rebind_xe >/dev/null 2>&1 || true
if grep -q '/bind' "$CALLS" && ! grep -q '/unbind' "$CALLS"; then
    ok "next attempt retries bind without unbinding again"
else
    bad "next attempt retries bind without unbinding again" "bind not retried"
fi

timeout() { echo "timeout $*" >> "$CALLS"; return 0; }
if rebind_xe && [[ ! -f "$PENDING" ]]; then
    ok "successful retry clears the pending target"
else
    bad "successful retry clears the pending target" "pending recovery remains"
fi

ln -s "$SYSFS/bus/pci/devices/0000:07:00.0" "$SYSFS/bus/pci/drivers/xe/0000:07:00.0"
fuser() { return 0; }
: > "$CALLS"
if ! rebind_xe && ! grep -q '^timeout ' "$CALLS" && grep -q '^start whisper-proxy$' "$CALLS"; then
    ok "busy device refuses rebind and restores voice"
else
    bad "busy device refuses rebind and restores voice" "unsafe rebind or lost proxy"
fi

echo "== health probes =="
curl() { echo 503; }
if llama_unresponsive; then bad "loading is left alone" "treated as hang"; else ok "loading is left alone"; fi
curl() { echo 200; }
if llama_unresponsive; then bad "healthy server is left alone" "treated as hang"; else ok "healthy server is left alone"; fi
curl() { echo call >> "$CALLS"; echo 000; return 28; }
: > "$CALLS"
if llama_unresponsive && [[ "$(wc -l < "$CALLS" | tr -d ' ')" == 3 ]]; then
    ok "three timed out health probes detect a stalled process"
else
    bad "three timed out health probes detect a stalled process" "did not require three probes"
fi
curl() { case "$*" in *'/slots'*) echo 000; return 28 ;; *) echo 200 ;; esac; }
if llama_unresponsive; then
    ok "healthy /health but silent /slots is a hung GPU kernel"
else
    bad "healthy /health but silent /slots is a hung GPU kernel" "missed"
fi
curl() { case "$*" in *'/slots'*) echo 501 ;; *) echo 200 ;; esac; }
if llama_unresponsive; then bad "disabled /slots still counts as alive" "treated as hang"; else ok "disabled /slots still counts as alive"; fi

echo "== stall confirmation =="
STUCK="$TMP/stuck"
if confirm_stuck 1; then bad "first stalled probe waits" "acted at once"; else ok "first stalled probe waits"; fi
if confirm_stuck 1; then bad "stall inside the grace period waits" "acted early"; else ok "stall inside the grace period waits"; fi
echo $(( $(date +%s) - 120 )) > "$STUCK"
if confirm_stuck 1; then ok "stall seen on two runs is confirmed"; else bad "stall seen on two runs is confirmed" "never confirmed"; fi
if ! confirm_stuck 0 && [[ ! -f "$STUCK" ]]; then
    ok "a good probe clears the stall"
else
    bad "a good probe clears the stall" "stall kept"
fi

echo "== wedge detection =="
journalctl() { printf 'xe 0000:07:00.0: [drm] *ERROR* CRITICAL: Xe has declared device 0000:07:00.0 as wedged.\n'; }
if xe_wedged_since 0; then ok "kernel wedge line is detected"; else bad "kernel wedge line is detected" "missed"; fi
journalctl() { printf 'xe 0000:07:00.0: [drm] GT0: reset done\n'; }
if xe_wedged_since 0; then bad "clean reset is not a wedge" "matched"; else ok "clean reset is not a wedge"; fi

echo "== reboot escalation =="
REBOOT_STAMP="$TMP/reboot"
echo 0000:07:00.0 > "$PENDING"
: > "$CALLS"
if reboot_box && grep -q '^reboot$' "$CALLS" && [[ ! -f "$PENDING" && -f "$REBOOT_STAMP" ]]; then
    ok "failed rebind reboots and drops the stale target"
else
    bad "failed rebind reboots and drops the stale target" "$(tr '\n' ' ' < "$CALLS")"
fi
: > "$CALLS"
if ! reboot_box && ! grep -q '^reboot$' "$CALLS"; then
    ok "second reboot inside the cooldown is refused"
else
    bad "second reboot inside the cooldown is refused" "rebooted again"
fi
rm -f "$REBOOT_STAMP"
media_busy() { return 0; }
: > "$CALLS"
if ! reboot_box && ! grep -q '^reboot$' "$CALLS"; then
    ok "running media job blocks the reboot"
else
    bad "running media job blocks the reboot" "rebooted under media"
fi
media_busy() { return 1; }

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
