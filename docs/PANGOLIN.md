# Pangolin remote-access configuration

HomeBrain reaches the internet through a [Pangolin](https://github.com/fosrl/pangolin)
tunnel: a `newt` client container on the box dials out to your Pangolin server, and you
map public hostnames ("resources") to local "targets" in the Pangolin dashboard. The **box
side is automatic** (`provision.sh`); the **Pangolin server side is manual**.

## Box side (automatic)

`provision.sh` (remote mode) writes the tunnel credentials and brings `newt` up:

```bash
# Repoint to a new tunnel / domain, keeping all data. Endpoint + factory
# password are inherited from the existing config when omitted.
sudo /opt/homebrain/scripts/provision.sh \
  --newt-id <ID> --newt-secret <SECRET> --domain <DOMAIN>
```

It stores `NEWT_ID` / `NEWT_SECRET` / `PANGOLIN_ENDPOINT` / `PANGOLIN_DOMAIN` in
`factory_config.txt` + `.env`, then (on an already-set-up box) redeploys, verifies the
tunnel connected, and prints the resource map below.

## Pangolin side (manual): resources → targets

Create one resource per service on the **site** that maps to this box's `newt` tunnel.
All targets are **HTTP** — TLS terminates at the Pangolin edge, so do **not** enable TLS to
the target.

| Public hostname  | Target              | Notes |
|------------------|---------------------|-------|
| `<domain>` (root)| `<gateway>:80`      | Dashboard / manager — a **host process**, so target the Docker bridge gateway, not a container name. |
| `nc.<domain>`    | `nextcloud:80`      | Nextcloud. Use container port **80**, **not** host-published 8080. |
| `ha.<domain>`    | `homeassistant:8123`| Home Assistant. |
| `vault.<domain>` | `vaultwarden:80`    | Vaultwarden. Host port 8082 is loopback-only — use the container name. |

**Why these targets:** `newt` runs on the `homebrain_default` Docker network with the Docker
socket mounted, so it reaches the service containers **by name** on their **internal** ports.
The host-published ports (8080 / 8123 / 8082) are a common trap — e.g. `nextcloud:8080` is
wrong (nothing listens on 8080 *inside* the container) and returns a 404.

Find the bridge gateway for the root/manager target:

```bash
docker network inspect homebrain_default --format '{{(index .IPAM.Config 0).Gateway}}'  # e.g. 172.18.0.1
```

## DNS

Point each hostname — `<domain>`, `nc.<domain>`, `ha.<domain>`, `vault.<domain>` (or a
wildcard `*.<domain>`) — at your Pangolin server's public IP.

## Verify

```bash
# Tunnel connected?
docker logs homebrain-newt-1 | grep "Tunnel connection to server established"

# Endpoints (after DNS + resources are in place):
for h in "" nc. ha. vault.; do
  curl -s -o /dev/null -w "%{http_code}  ${h}<domain>\n" "https://${h}<domain>"
done
```

Expected: manager `401` (login page), `nc` `302`, `ha` `200`, `vault` `200`. A `404` means
Pangolin has no route for that hostname (resource missing or wrong site); a connection
failure / cert error means DNS or the resource isn't set up yet.
