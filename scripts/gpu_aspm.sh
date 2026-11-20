#!/bin/bash
# Let an Intel Arc reach its low-power idle states.
#
# An Arc needs PCIe ASPM L1 on its link before its package can idle deeper
# than G2. Many boards ship with ASPM off. Linux then keeps the BIOS setting,
# and the card idles at full link power. On .69 (Arc Pro B60 on a B550 board)
# that was 33 W, 70 °C and a blower at 2,070 rpm with the GPU doing nothing.
# With L1 on the card's links it went to 7 W, 45 °C and 541 rpm, and the
# package sat in G8.
#
# Only the links between the Arc and its root port get L1, and only L1: no
# L0s, no L1 substates, no clock power management, because those need CLKREQ#,
# which a riser cable may not carry. The kernel only turns ASPM on under a
# policy other than "default", and a policy applies to every link. So every
# control that is off on any other link is locked off first, and those links
# keep exactly what the BIOS gave them.
#
# Idempotent. recover_gpu.sh runs it again after a rebind.

SYSFS="${SYSFS:-/sys}"
POLICY="$SYSFS/module/pcie_aspm/parameters/policy"

say() { echo "$(date -Is) $*"; }

# PCI addresses of Intel display controllers bound to xe.
arc_devices() {
    local link
    for link in "$SYSFS"/bus/pci/drivers/xe/????:??:??.?; do
        [[ -e "$link" ]] && basename "$link"
    done
}

# bdf -> the devices from it up to, not including, its root port. Each one's
# link/ controls the link to its parent.
arc_path() {
    local dev comp
    dev=$(readlink -f "$SYSFS/bus/pci/devices/$1") || return 1
    while [[ "$(basename "$(dirname "$dev")")" != pci* ]]; do
        comp=$(basename "$dev")
        echo "$comp"
        dev=$(dirname "$dev")
    done
}

main() {
    [[ -w "$POLICY" ]] || { say "kernel ASPM control is not available; nothing to do"; return 0; }
    local -A on=()
    local bdf d l f
    for bdf in $(arc_devices); do
        for d in $(arc_path "$bdf"); do on["$d"]=1; done
    done
    if [[ ${#on[@]} -eq 0 ]]; then
        say "no Arc on xe; nothing to do"
        return 0
    fi
    for l in "$SYSFS"/bus/pci/devices/*/link; do
        [[ -d "$l" ]] || continue
        d=$(basename "$(dirname "$l")")
        if [[ -n "${on[$d]:-}" ]]; then
            for f in l1_2_pcipm l1_2_aspm l1_1_pcipm l1_1_aspm l0s_aspm clkpm; do
                [[ -w "$l/$f" ]] && echo 0 > "$l/$f"
            done
            [[ -w "$l/l1_aspm" ]] && echo 1 > "$l/l1_aspm"
        else
            for f in l1_2_pcipm l1_2_aspm l1_1_pcipm l1_1_aspm l1_aspm l0s_aspm clkpm; do
                [[ -r "$l/$f" && "$(cat "$l/$f")" == 0 ]] && echo 0 > "$l/$f"
            done
        fi
    done
    if [[ "$(cat "$POLICY")" != *"[powersave]"* ]]; then
        echo powersave > "$POLICY" || { say "could not set the ASPM policy"; return 1; }
    fi
    for d in "${!on[@]}"; do
        l="$SYSFS/bus/pci/devices/$d/link/l1_aspm"
        [[ -r "$l" ]] && say "ASPM L1 on $d: $(cat "$l")"
    done
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
fi
