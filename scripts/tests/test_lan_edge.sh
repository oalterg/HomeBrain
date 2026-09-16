#!/bin/bash
# Destructive only to disposable CI networking: run on an isolated Linux runner.
# Real Caddy TLS/routing, kernel ingress filtering, Avahi, and systemd cgroups.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
cleanup() {
    systemctl stop homebrain-lan-test-parent homebrain-update 2>/dev/null || true
    rm -f /etc/systemd/system/homebrain-lan-test-parent.service
    systemctl daemon-reload
    docker rm -f hb-edge-test hb-backend-test >/dev/null 2>&1 || true
    docker network rm hb-edge-test >/dev/null 2>&1 || true
    ip netns del hb-lan-test 2>/dev/null || true
    ip link del hb-lan-test 2>/dev/null || true
    kill "${manager_pid:-}" "${mdns_pid:-}" 2>/dev/null || true
    iptables -w -D INPUT -p tcp --dport 8000 -j HOMEBRAIN_MANAGER 2>/dev/null || true
    iptables -w -F HOMEBRAIN_MANAGER 2>/dev/null || true
    iptables -w -X HOMEBRAIN_MANAGER 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

# Test the actual updater handoff without downloading/upgrading the CI host.
sed '/^SCRIPT_DIR=/,$d' "$ROOT/scripts/update.sh" > "$TMP/update-probe.sh"
cat >> "$TMP/update-probe.sh" <<EOF
systemctl stop homebrain-lan-test-parent
sleep 1
touch "$TMP/survived"
EOF
cat > /etc/systemd/system/homebrain-lan-test-parent.service <<EOF
[Service]
ExecStart=/bin/bash $TMP/update-probe.sh
EOF
systemctl daemon-reload
systemctl start homebrain-lan-test-parent
for _ in {1..30}; do [[ -f "$TMP/survived" ]] && break; sleep 1; done
[[ -f "$TMP/survived" ]]
echo 'PASS updater survives its parent service stopping'

# Install guard twice to exercise idempotence before opening the host backend.
bash "$ROOT/scripts/manager_firewall.sh"
bash "$ROOT/scripts/manager_firewall.sh"
[[ $(iptables -S INPUT | grep -c -- '-j HOMEBRAIN_MANAGER') == 1 ]]
printf 'manager\n' > "$TMP/index.html"
python3 -m http.server 8000 --bind 0.0.0.0 --directory "$TMP" > "$TMP/manager.log" 2>&1 &
manager_pid=$!
for _ in {1..20}; do curl -fsS http://127.0.0.1:8000/ && break; sleep 1; done

# A LAN source inside 172.16/12 must still be denied; match ingress, not CIDR.
ip netns add hb-lan-test
ip link add hb-lan-test type veth peer name hb-client
ip link set hb-client netns hb-lan-test
ip addr add 172.29.250.1/30 dev hb-lan-test
ip link set hb-lan-test up
ip netns exec hb-lan-test ip addr add 172.29.250.2/30 dev hb-client
ip netns exec hb-lan-test ip link set hb-client up
ip netns exec hb-lan-test ip link set lo up
if ip netns exec hb-lan-test curl --noproxy '*' -fsS --max-time 3 http://172.29.250.1:8000/; then
    echo 'FAIL LAN reached plaintext manager'; exit 1
fi
echo 'PASS LAN blocked, loopback allowed'

# A tiny real HTTP backend echoes Host and protocol. All app aliases share it;
# distinct Host values prove Caddy selected and forwarded the intended site.
cat > "$TMP/backend.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write((self.headers['Host'] + ' ' + self.headers.get('X-Forwarded-Proto', '')).encode())
HTTPServer(('0.0.0.0', 80), Handler).serve_forever()
PY
# HA has a separate internal port.
cat > "$TMP/start-backend.sh" <<'EOF'
python /backend.py &
sed 's/, 80)/, 8123)/' /backend.py > /ha.py
python /ha.py
EOF
docker network create hb-edge-test >/dev/null
docker run -d --name hb-backend-test --network hb-edge-test \
    --network-alias nextcloud --network-alias vaultwarden --network-alias homeassistant \
    -v "$TMP/backend.py:/backend.py:ro" -v "$TMP/start-backend.sh:/start.sh:ro" \
    python:3.12-alpine sh /start.sh >/dev/null
docker run -d --name hb-edge-test --network hb-edge-test \
    --add-host host.docker.internal:host-gateway -p 80:80 -p 443:443 \
    -v "$ROOT/config/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.4-alpine >/dev/null
for _ in {1..30}; do
    curl -kfsS https://127.0.0.1/healthz && break
    sleep 1
done
docker cp hb-edge-test:/data/caddy/pki/authorities/local/root.crt "$TMP/ca.crt"
for name in homebrain.local nc-homebrain.local vault-homebrain.local ha-homebrain.local; do
    body=$(curl --noproxy '*' --cacert "$TMP/ca.crt" --resolve "$name:443:127.0.0.1" -fsS "https://$name/")
    if [[ "$name" == homebrain.local ]]; then
        [[ "$body" == manager ]]
    else
        [[ "$body" == "$name https" ]]
    fi
    location=$(curl --noproxy '*' --resolve "$name:80:127.0.0.1" -sSI "http://$name/path?x=1" | tr -d '\r')
    grep -q "Location: https://$name/path?x=1" <<< "$location"
done
# Pangolin uses the same bridge-to-host route without TLS at this hop.
docker exec hb-edge-test wget -qO- http://host.docker.internal:8000/ | grep -q manager
echo 'PASS all four TLS names, redirects, and bridge access'

systemctl start avahi-daemon
bash "$ROOT/scripts/publish_mdns.sh" > "$TMP/mdns.log" 2>&1 &
mdns_pid=$!
for name in nc-homebrain.local vault-homebrain.local ha-homebrain.local; do
    for _ in {1..20}; do getent ahostsv4 "$name" > "$TMP/resolved" && break; sleep 1; done
    [[ -s "$TMP/resolved" ]]
done
echo 'PASS flat names resolve through the Linux system resolver'
