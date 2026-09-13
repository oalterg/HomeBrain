#!/bin/bash
# Publish A records for nc/vault/ha.homebrain.local via Avahi.
#
# homebrain.local is the machine hostname (avahi-daemon already advertises
# it). The three aliases do not exist until something publishes them —
# Caddy SANs are not DNS. avahi-publish stays in the foreground; systemd
# restarts the unit when the LAN IP changes (refresh_vault_lan_ip).
set -euo pipefail

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -n "$ip" ]] || exit 1

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT

for name in nc.homebrain.local vault.homebrain.local ha.homebrain.local; do
	avahi-publish -a -R "$name" "$ip" &
	pids+=($!)
done

# Any publisher dying (IP gone, avahi restarted) takes the unit down so
# systemd brings all three back on the current address.
wait -n "${pids[@]}"
exit 1
