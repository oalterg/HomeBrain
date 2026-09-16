#!/bin/bash
# Flat .local names work with nss-mdns's two-label limit. Republish when the
# multicast route's source changes; never select a Docker/loopback address.
set -euo pipefail
pids=()
cleanup() {
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
    pids=()
}
trap cleanup EXIT
trap 'exit 0' TERM INT
current_ip=""
while true; do
    ip=$(ip -4 route get 224.0.0.251 2>/dev/null | awk '
        {for (i=1;i<=NF;i++) {if ($i=="dev") dev=$(i+1); if ($i=="src") src=$(i+1)}}
        END {if (dev!="lo" && dev!~/^(docker|br-|veth)/) print src}') || ip=""
    alive=true
    for pid in "${pids[@]}"; do
        kill -0 "$pid" 2>/dev/null || alive=false
    done
    if [[ "$ip" != "$current_ip" || "$alive" == false ]]; then
        cleanup
        current_ip="$ip"
        if [[ -n "$ip" ]]; then
            for name in nc-homebrain.local vault-homebrain.local ha-homebrain.local; do
                avahi-publish -a -R "$name" "$ip" &
                pids+=($!)
            done
        fi
    fi
    sleep 5 &
    wait $! || true
done
