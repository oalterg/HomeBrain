#!/bin/bash
# Host gunicorn is reachable only through loopback or Docker bridge ingress.
# Run before binding :8000; fail closed if rules cannot be installed. A source
# CIDR alone also permits LAN clients on that subnet, so match interfaces.
set -euo pipefail
iptables -w -N HOMEBRAIN_MANAGER 2>/dev/null || iptables -w -S HOMEBRAIN_MANAGER >/dev/null
# Insert the terminal reject first. Repeated starts add no duplicate rules.
for rule in '-j REJECT' '-i br+ -j ACCEPT' '-i docker0 -j ACCEPT' '-i lo -j ACCEPT'; do
    # Intentional splitting: each rule above is a fixed argument list.
    # shellcheck disable=SC2086
    iptables -w -C HOMEBRAIN_MANAGER $rule 2>/dev/null || iptables -w -I HOMEBRAIN_MANAGER 1 $rule
done
iptables -w -C INPUT -p tcp --dport 8000 -j HOMEBRAIN_MANAGER 2>/dev/null \
    || iptables -w -I INPUT 1 -p tcp --dport 8000 -j HOMEBRAIN_MANAGER
