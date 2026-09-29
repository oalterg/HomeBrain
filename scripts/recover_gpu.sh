#!/bin/bash
# Bring llama-server back after systemd has given up on it.
#
# A wedged Arc does not stay restartable. The xe reset fails, SYCL then
# reports no device, and StartLimitBurst is spent on instant exits. The unit
# sits failed until somebody rebinds the PCI device. This script does that
# once, then starts chat again. A picture or video job has stopped chat on
# purpose, so those hold the GPU until they finish.
#
# A kernel can also hang without any error. Level Zero runs compute in
# long-running mode, which has no job timeout, so llama-server spins forever
# while /health still says ok. Killing it then wedges the device. On the Arc
# Pro B60 the GuC firmware then fails authentication on every rebind, and
# only a reboot brings the GPU back.

STAMP=/var/lib/homebrain/gpu-recover.stamp
PENDING=/var/lib/homebrain/gpu-recover.pci
STUCK=/var/lib/homebrain/gpu-recover.stuck
REBOOT_STAMP=/var/lib/homebrain/gpu-recover.reboot
SYSFS=/sys
DRI=/dev/dri
COOLDOWN=600
# A stall must be seen by two runs before chat is killed. A run with dead
# probes takes about 25s, so the next run lands roughly 85s later.
STUCK_GRACE=50
# Never reboot-loop the whole box over one bad card.
REBOOT_COOLDOWN=21600

say() { echo "$(date -Is) $*"; }

# state result media_busy cooldown nodevice unresponsive -> action name
recover_action() {
    local state="$1" result="$2" media="$3" cooldown="$4" nodevice="$5"
    local unresponsive="${6:-0}"
    if [[ "$media" == 1 ]]; then
        echo skip-media
        return 0
    fi
    if [[ "$state" == "active" && "$unresponsive" == 0 ]] \
        || [[ "$state" == "activating" || "$state" == "reloading" ]]; then
        echo skip-up
        return 0
    fi
    # picture.sh stops llama-server cleanly. Leave that stop alone.
    if [[ "$state" == "inactive" && "$result" == "success" && "${7:-0}" == 0 ]]; then
        echo skip-stopped
        return 0
    fi
    if [[ "$state" != "failed" && "$state" != "active" && "${7:-0}" == 0 ]]; then
        echo skip-up
        return 0
    fi
    if [[ "$cooldown" == 1 ]]; then
        echo skip-cooldown
        return 0
    fi
    if [[ "$nodevice" == 1 ]]; then
        echo rebind
        return 0
    fi
    echo restart
}

log_says_no_device() {
    case "$1" in
        *"No device of requested type available"*) return 0 ;;
        *) return 1 ;;
    esac
}

media_busy() {
    systemctl is-active --quiet homebrain-picture.service && return 0
    systemctl is-active --quiet homebrain-video.service && return 0
    # The character class keeps pgrep from matching its own command line.
    pgrep -f '[C]omfyUI/.venv/bin/python main.py' >/dev/null 2>&1 && return 0
    return 1
}

# file seconds -> true while the epoch in file is younger than seconds
stamp_fresh() {
    local file="$1" limit="$2" now then
    [[ -f "$file" ]] || return 1
    now=$(date +%s)
    then=$(cat "$file" 2>/dev/null || echo 0)
    case "$then" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [[ $((now - then)) -lt $limit ]]
}

cooldown_active() {
    stamp_fresh "$STAMP" "$COOLDOWN"
}

mark_cooldown() {
    mkdir -p "$(dirname "$STAMP")"
    date +%s > "$STAMP"
}

# Call once per run with the probe result. True only when the stall was also
# seen at least STUCK_GRACE seconds ago, so one slow probe never kills chat.
confirm_stuck() {
    if [[ "$1" != 1 ]]; then
        rm -f "$STUCK"
        return 1
    fi
    if [[ ! -f "$STUCK" ]]; then
        mkdir -p "$(dirname "$STUCK")"
        date +%s > "$STUCK"
        say "llama-server stopped answering /slots; checking again next run"
        return 1
    fi
    ! stamp_fresh "$STUCK" "$STUCK_GRACE"
}

# epoch -> true when xe declared the device wedged after that time
xe_wedged_since() {
    journalctl -k -b --since "@$1" -o cat --no-pager 2>/dev/null \
        | grep -q 'as wedged'
}

reboot_box() {
    media_busy && { say "media owns the GPU; not rebooting"; return 1; }
    if stamp_fresh "$REBOOT_STAMP" "$REBOOT_COOLDOWN"; then
        say "already rebooted for the GPU in the last $((REBOOT_COOLDOWN / 3600))h; leaving it wedged"
        return 1
    fi
    mkdir -p "$(dirname "$REBOOT_STAMP")"
    date +%s > "$REBOOT_STAMP"
    # A reboot probes the device again, so the half-done rebind is moot.
    rm -f "$PENDING" "$STUCK"
    say "the Arc stays wedged after a rebind; rebooting"
    systemctl reboot
}

xe_bdf() {
    local link bdf found=""
    # A failed bind removes the driver symlink. Keep the PCI address until a
    # later bind succeeds, but never attach a device taken by another driver.
    if [[ -f "$PENDING" ]]; then
        read -r bdf < "$PENDING"
        [[ "$bdf" =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]] || return 1
        link="$SYSFS/bus/pci/devices/$bdf"
        [[ "$(cat "$link/vendor" 2>/dev/null)" == 0x8086 ]] || return 1
        if [[ -L "$link/driver" ]]; then
            [[ "$(basename "$(readlink "$link/driver")")" == xe ]] || return 1
        fi
        echo "$bdf"
        return 0
    fi
    for link in "$SYSFS"/bus/pci/drivers/xe/????:??:??.?; do
        [[ -e "$link" ]] || continue
        # Do not guess which GPU llama uses on a multi-xe system.
        [[ -z "$found" ]] || return 1
        found=$(basename "$link")
    done
    [[ -n "$found" ]] || return 1
    echo "$found"
}

render_busy() {
    local node
    for node in "$SYSFS/bus/pci/devices/$1/drm/"*; do
        [[ -e "$node" ]] || continue
        fuser "$DRI/$(basename "$node")" >/dev/null 2>&1 && return 0
    done
    return 1
}

rebind_xe() (
    local bdf whisper=0 proxy=0 i path
    command -v fuser >/dev/null 2>&1 || { say "fuser is required before rebinding"; return 1; }
    bdf=$(xe_bdf) || { say "no xe device to rebind"; return 1; }
    media_busy && { say "media owns the GPU; not rebinding"; return 1; }
    systemctl is-active --quiet whisper-server && whisper=1
    systemctl is-active --quiet whisper-proxy && proxy=1
    # Stopping the server also stops its Requires= proxy. Restore both on
    # every exit, including failed unbind/bind and service termination.
    trap '[[ "$whisper" == 0 ]] || systemctl start whisper-server; [[ "$proxy" == 0 ]] || systemctl start whisper-proxy' EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    if [[ "$whisper" == 1 || "$proxy" == 1 ]]; then
        systemctl stop whisper-server || return 1
    fi
    i=0
    while render_busy "$bdf" && [[ $i -lt 15 ]]; do
        sleep 1
        i=$((i + 1))
    done
    if render_busy "$bdf"; then
        say "render node still busy; not rebinding"
        return 1
    fi
    path="$SYSFS/bus/pci/drivers/xe"
    mkdir -p "$(dirname "$PENDING")"
    echo "$bdf" > "$PENDING" || return 1
    if [[ -L "$SYSFS/bus/pci/devices/$bdf/driver" ]]; then
        if ! timeout -k 5 25 bash -c 'echo "$1" > "$2"' _ "$bdf" "$path/unbind"; then
            say "unbind $bdf failed"
            return 1
        fi
        sleep 2
    fi
    if ! timeout -k 5 25 bash -c 'echo "$1" > "$2"' _ "$bdf" "$path/bind"; then
        say "bind $bdf failed"
        return 1
    fi
    rm -f "$PENDING"
    sleep 1
    say "rebound $bdf"
)

llama_unresponsive() {
    local attempt code alive=0
    # /health remains responsive during inference. 503 is normal during
    # model loading; never confuse that with a wedged process.
    for attempt in 1 2 3; do
        code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/health)
        case "$code" in 200|503) alive=1; break ;; esac
        [[ "$attempt" == 3 ]] || sleep 5
    done
    [[ "$alive" == 1 ]] || return 0
    # /health stays 200 while a GPU kernel never returns. /slots answers in
    # about a millisecond even mid-prompt, but not at all during that hang.
    # Any HTTP status, including a disabled endpoint, means it is alive.
    code=$(curl -s --max-time 15 -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/slots)
    [[ "$code" == 000 ]]
}

start_llama() {
    systemctl reset-failed llama-server || true
    systemctl start llama-server || say "llama-server start failed"
}

main() {
    command -v systemctl >/dev/null 2>&1 || return 0
    systemctl cat llama-server.service >/dev/null 2>&1 || return 0

    local state result media=0 cooldown=0 nodevice=0 unresponsive=0 pending=0 action text invocation since
    state=$(systemctl show -p ActiveState --value llama-server)
    result=$(systemctl show -p Result --value llama-server)
    media_busy && media=1
    cooldown_active && cooldown=1
    if [[ "$state" == active && "$media" == 0 && "$cooldown" == 0 ]]; then
        llama_unresponsive && unresponsive=1
        confirm_stuck "$unresponsive" || unresponsive=0
    fi
    # Restrict evidence to the current invocation, not an earlier outage.
    invocation=$(systemctl show -p InvocationID --value llama-server)
    text=""
    [[ -z "$invocation" ]] || text=$(journalctl -b "_SYSTEMD_INVOCATION_ID=$invocation" -n 40 --no-pager 2>/dev/null || true)
    log_says_no_device "$text" && nodevice=1
    if [[ -f "$PENDING" ]]; then nodevice=1; pending=1; fi
    # The rebind already failed after the one reboot we allow. Retrying every
    # cooldown only bounces voice for a bind that cannot succeed.
    if [[ "$pending" == 1 ]] && stamp_fresh "$REBOOT_STAMP" "$REBOOT_COOLDOWN"; then
        return 0
    fi
    action=$(recover_action "$state" "$result" "$media" "$cooldown" "$nodevice" "$unresponsive" "$pending")
    case "$action" in
        rebind)
            mark_cooldown
            say "llama-server failed and SYCL sees no device; rebinding the Arc"
            systemctl stop llama-server || return 1
            rebind_xe || { say "xe rebind failed"; reboot_box; return 1; }
            start_llama
            ;;
        restart)
            mark_cooldown
            rm -f "$STUCK"
            say "llama-server failed or stopped responding; restarting it"
            since=$(date +%s)
            systemctl stop llama-server || return 1
            # Killing a hung kernel is what wedges the device. Starting on
            # a wedged Arc only burns the start limit, so rebind now.
            if xe_wedged_since "$since"; then
                say "xe declared the Arc wedged; rebinding"
                rebind_xe || { say "xe rebind failed"; reboot_box; return 1; }
            fi
            start_llama
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
fi
