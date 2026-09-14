# LAN HTTPS — names, not ports

**Status:** in tree (2026-09-13). Caddy is the LAN face on 443; apps are
loopback or compose-network only. Avahi publishes `nc-homebrain.local`,
`vault-homebrain.local`, and `ha-homebrain.local`.
**Date:** 2026-09-13
**Essence:** The LAN uses one flat `.local` name per app. Every owner-facing
URL is HTTPS on 443, no port. Caddy is the only process that faces the
LAN. Telegram stays the away-from-home clerk (documents ≤20 MB). Pangolin
stays optional, for the same apps from outside.

Related: [`PANGOLIN.md`](../PANGOLIN.md) (the public name map this mirrors),
[`VAULT_PLAN.md`](VAULT_PLAN.md) (LAN HTTPS + internal CA, ported),
`config/Caddyfile`, `scripts/common.sh` `configure_nc_ha_proxy_settings`,
`scripts/publish_mdns.sh`.

---

## 1. Why

Nextcloud and Bitwarden clients require HTTPS. Home Assistant's companion
app allows HTTP on the LAN; the product does not. A phone must not be
told `http://homebrain.local` or `https://homebrain.local:8444`.

Ports are an implementation leak. The tunnel already teaches one pattern:
one hostname per service, TLS at the edge. The LAN should be that pattern.

---

## 2. The map

| Service | At home | Away (public tunnel on) |
|---|---|---|
| Dashboard | `https://homebrain.local` | `https://<domain>` |
| Nextcloud | `https://nc-homebrain.local` | `https://nc.<domain>` |
| Vault | `https://vault-homebrain.local` | `https://vault.<domain>` |
| Home Assistant | `https://ha-homebrain.local` | `https://ha.<domain>` |

HTTP on port 80 exists only to redirect to HTTPS. Telegram is not in this
table: on a GPU box it is the daily remote path, not a web UI.

The Nextcloud app stores **one** server URL.

- **Local network** (GPU default): pairing is `https://nc-homebrain.local`
  for the life of that pairing. Off-LAN it does not sync. Telegram covers
  the clerk; the library waits.
- **Public tunnel on:** pairing is `https://nc.<domain>`. Flip later →
  re-pair or re-point the app. Do not keep two Nextcloud URLs silent.

---

## 3. What the box must do

Caddy listens on **443** (and 80 for the redirect). It routes by `Host`.
Nextcloud, Vaultwarden, Home Assistant, and the dashboard are not
published on the LAN. They stay on the Docker network or loopback so
healthchecks, MCP, and self-test can still use `127.0.0.1`.

| Name | Backend |
|---|---|
| `homebrain.local` | host gunicorn on `:8000`, reached via Docker bridge gateway |
| `nc-homebrain.local` | `nextcloud:80` |
| `vault-homebrain.local` | `vaultwarden:80` |
| `ha-homebrain.local` | `homeassistant:8123` (WebSocket upgrade on) |

One Caddy internal CA, installed once per phone, labelled as **this box's
certificate** — not a Vault CA. It covers all four names. The download
is available whenever the dashboard is opened on the LAN, including a
remote-mode box reached at `homebrain.local`.

Nextcloud (local mode): `overwriteprotocol=https`,
`overwrite.cli.url=https://nc-homebrain.local`, trusted domain
`nc-homebrain.local` (and `homebrain.local` / LAN IP as needed).
Vault `DOMAIN=https://vault-homebrain.local`. HA `use_x_forwarded_for`
and `trusted_proxies` already exist for the tunnel; they cover Caddy too.

### Names have to resolve

`homebrain.local` is the appliance hostname. The three app aliases are
published explicitly. Flat names avoid nss-mdns's two-label limit; see
[resolver documentation](https://github.com/avahi/nss-mdns#activation).
The publisher checks the IPv4 multicast route every five seconds, withdraws
old records when the source address disappears, and republishes after an
address change or publisher failure. Docker and loopback interfaces are excluded.
The host's managed `/etc/hosts` block separately directs app names to loopback.

A raw IP on 443 opens the dashboard only. Clients must be on a LAN that
allows mDNS (UDP 5353 multicast), without guest/client isolation. If a client
cannot resolve the names, first disable VPN/DNS filtering and try the main
Wi-Fi. An administrator can supply local DNS or client hosts entries mapping
all four names to a reserved LAN address. Changing just the URL to an IP
cannot select the apps. Networks without this resolution path are not supported
for LAN pairing; an optional public tunnel remains available.

Gunicorn's pre-start firewall guard accepts port 8000 only on loopback and
Docker bridge ingress, then rejects every other interface. It runs before
binding and fails startup if filtering cannot be installed. UFW is optional;
no broad private-source-CIDR allowance is needed. Other host services (SSH,
configured camera FTP) retain their own policies.

---

## 4. Exceptions (keep them named)

- **Cameras and other IoT** that cannot do mDNS or TLS still use a raw
  IP. Device limit, not the owner's URL bar. The Connectivity card
  already says this.
- **Loopback ports** for `healthcheck`, self-test, and MCP stay. They are
  not printed, QR-coded, or bookmarked.
- **No fourth transport.** No Telegram chunking, no local Bot API, no
  mandatory Pangolin so a phone can skip the CA.

---

## 5. What was true before (do not document as the target)

Replaced, not a third door: Nextcloud HTTP `:8080` / HTTPS `:8444`, Vault
HTTPS `:8443`, HA HTTP `:8123`, dashboard HTTP `:80`, pairing QR
`https://homebrain.local:8444`, CA download Vault-branded and 404 in
remote mode. See git history if you are reading an old box.

---

## 6. Upgrade and client onboarding

1. Arrange local console/SSH access and a backup before upgrading a remote box.
   Record the Pangolin manager target and keep its resource editor open.
2. Start the update. It hands off to `homebrain-update.service`, which survives
   the manager restart. The manager releases port 80 before Compose gives it to
   Caddy. The updater waits for Nextcloud before clearing the old `overwritehost`.
   Follow `/var/log/homebrain/manager_update.log` or
   `journalctl -u homebrain-update` if the dashboard disconnects.
3. Change only Pangolin's manager target from `<gateway>:80` to
   `<gateway>:8000` after the manager moves. Expect a brief remote outage between
   those steps. Verify the public dashboard and all LAN app names. App tunnel
   targets (`nextcloud:80`, `homeassistant:8123`, `vaultwarden:80`) stay the same.
4. Keep the `caddy_data` volume: it contains the existing box CA. Obtain
   **This box's certificate** from the authenticated dashboard. On first setup,
   use a trusted LAN; an administrator can instead copy the CA over SSH before
   entering credentials in the browser. Do not install an unknown box's CA.
5. Install the CA on each device. On iOS/iPadOS, installing the profile is not
   enough: enable full trust in Settings → General → About → Certificate Trust
   Settings ([Apple instructions](https://support.apple.com/en-ie/102390)).
   For Android and desktop clients follow the platform-specific
   [Bitwarden certificate instructions](https://bitwarden.com/help/certificates/).
   Verify trust in each native app as well as the browser; browser acceptance
   alone is not a native-client compatibility test.
6. Sync pending changes before repointing existing clients. Replace old
   `:8443`/`:8444` URLs (or the experimental dotted aliases) with the flat names
   above. Re-pair Nextcloud, preserving local unsynced photos; update Bitwarden's
   self-hosted server and Home Assistant's internal URL. Public pairings continue
   using their public URL. Switching local/remote mode requires deliberate
   client re-pairing when changing that stored URL.

If migration fails, use SSH/console and the updater log; port 8000 is deliberately
unavailable on the LAN. Fix the failed step and rerun the update. Do not delete
volumes or downgrade Nextcloud as a network-recovery measure.

### Acceptance gates

CI runs real Caddy TLS/Host routing and redirects, a simulated LAN ingress
against the kernel firewall, Linux system name resolution, and an updater
handoff that survives its parent service stopping. Stateful tests cover
local → remote → local Nextcloud configuration and live publisher IP changes.

Before appliance rollout, verify on actual iOS, Android, and desktop clients:
CA installation, native Nextcloud/Bitwarden/HA login and sync, HA WebSockets,
a dashboard-triggered upgrade from the previous release, and DHCP renewal.
Record device/OS/app versions. These hardware/native-client checks are manual;
the CI HTTP backends do not claim to exercise the full applications.

## 7. Pointers

| What | Where |
|---|---|
| Public name map | [`PANGOLIN.md`](../PANGOLIN.md) |
| Caddy (Host on 443) | `config/Caddyfile` |
| NC overwrite / trusted domains | `scripts/common.sh` `configure_nc_ha_proxy_settings` |
| Pairing URL | `src/app.py` `nc_client_url` |
| Box CA download | `src/app.py` `/api/vault/local-ca` |
| LAN IP for cert SAN | `scripts/common.sh` `refresh_vault_lan_ip` |
| Avahi aliases | `scripts/publish_mdns.sh` |
| 20 MB remote clerk | workspace `AGENTS.md` `## HomeBrain files` |
