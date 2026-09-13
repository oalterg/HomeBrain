# LAN HTTPS — names, not ports

**Status:** decided, not in tree (2026-09-13). Today's box still publishes
`:8080` / `:8123` / `:8443` / `:8444` and only advertises `homebrain.local`.
**Date:** 2026-09-13
**Essence:** The LAN is Pangolin with a `.local` suffix. Every owner-facing
URL is HTTPS on 443, no port. Caddy is the only process that faces the
LAN. Telegram stays the away-from-home clerk (documents ≤20 MB). Pangolin
stays optional, for the same apps from outside.

Related: [`PANGOLIN.md`](../PANGOLIN.md) (the public name map this mirrors),
[`VAULT_PLAN.md`](VAULT_PLAN.md) (LAN HTTPS + internal CA, ported),
[`PRODUCT_REVIEW_2026-07.md`](PRODUCT_REVIEW_2026-07.md) §B.2 (dashboard
still HTTP), `config/Caddyfile`, `scripts/common.sh`
`configure_nc_ha_proxy_settings`.

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
| Nextcloud | `https://nc.homebrain.local` | `https://nc.<domain>` |
| Vault | `https://vault.homebrain.local` | `https://vault.<domain>` |
| Home Assistant | `https://ha.homebrain.local` | `https://ha.<domain>` |

HTTP on port 80 exists only to redirect to HTTPS. Telegram is not in this
table: on a GPU box it is the daily remote path, not a web UI.

The Nextcloud app stores **one** server URL.

- **Local network** (GPU default): pairing is `https://nc.homebrain.local`
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
| `homebrain.local` | host gunicorn (today `:80`; reach it the way Pangolin already does — Docker bridge gateway) |
| `nc.homebrain.local` | `nextcloud:80` |
| `vault.homebrain.local` | `vaultwarden:80` |
| `ha.homebrain.local` | `homeassistant:8123` (WebSocket upgrade on) |

One Caddy internal CA, installed once per phone, labelled as **this box's
certificate** — not a Vault CA. It covers all four names. The download
is available whenever the dashboard is opened on the LAN, including a
remote-mode box reached at `homebrain.local`.

Nextcloud (local mode): `overwriteprotocol=https`,
`overwrite.cli.url=https://nc.homebrain.local`, trusted domain
`nc.homebrain.local` (and `homebrain.local` / LAN IP as needed).
Vault `DOMAIN=https://vault.homebrain.local`. HA `use_x_forwarded_for`
and `trusted_proxies` already exist for the tunnel; they cover Caddy too.

### Names have to resolve

`homebrain.local` is the machine hostname. **`nc.` / `vault.` / `ha.` do
not exist until we publish them.** Caddy SANs are not DNS. A small Avahi
publisher (A records for the three aliases, refreshed when the LAN IP
changes — same moment as `refresh_vault_lan_ip`) is the new mechanism.
Without it this map is fiction.

A raw IP on 443 can only be one service (the dashboard, or a page that
lists the four names). Phones that cannot resolve `.local` are told to
use the name, not `https://192.168.x.x`.

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

## 5. What is true today (do not document as the target)

- Nextcloud HTTP `:8080` and HTTPS `:8444`; local mode forces
  `overwriteprotocol=http` and `overwrite.cli.url=http://homebrain.local:8080`.
- Vault HTTPS `:8443`; CA download is Vault-branded and 404s when
  `DEPLOYMENT_MODE=remote`.
- Home Assistant HTTP `:8123`. Dashboard HTTP `:80`.
- Caddy SANs already list `nc.homebrain.local` and `vault.homebrain.local`;
  nothing publishes those names.
- Pairing QR in local mode is `https://homebrain.local:8444`.

Replace those, do not add a third door.

---

## 6. Pointers

| What | Where |
|---|---|
| Public name map | [`PANGOLIN.md`](../PANGOLIN.md) |
| Today's Caddy (ports) | `config/Caddyfile` |
| NC overwrite / trusted domains | `scripts/common.sh` `configure_nc_ha_proxy_settings` |
| Pairing URL | `src/app.py` `nc_client_url` |
| LAN CA download | `src/app.py` `/api/vault/local-ca` |
| LAN IP for cert SAN | `scripts/common.sh` `refresh_vault_lan_ip` |
| 20 MB remote clerk | workspace `AGENTS.md` `## HomeBrain files` |
