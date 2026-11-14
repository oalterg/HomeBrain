# Remote access — no HomeBrain server, nothing a third party can read

**Status:** in tree (2026-11-14), **not proven end to end.** The join, the
dashboard card and the pinned install are walked on .69 up to the sign-in
link. Everything after sign-in — publishing, the apps behind it, the public
address, the lock — is unit-tested against a stand-in and checked against
the pinned client's source, and has never run on a signed-in box. The list is
in [`TESTING.md`](../TESTING.md#remote-access-private-network). §9 says what
was built and where it differs from the proposal below.
**Date:** 2026-11-13, built 2026-11-14
**Essence:** The box joins a mesh VPN on the owner's own account. Private
mesh carries the dashboard and box-to-box backup. A public address whose
encryption ends on the box carries the three phone apps. No server in the
path can decipher the traffic, and HomeBrain runs none of them.

Related: [`PRODUCT_REVIEW_2026-11.md`](PRODUCT_REVIEW_2026-11.md) (why this is
the missing element), [`LAN_HTTPS.md`](LAN_HTTPS.md) (the LAN face this must
sit beside), [`PANGOLIN.md`](../PANGOLIN.md) (what it replaces),
[`HOUSEHOLD_ACCOUNTS.md`](HOUSEHOLD_ACCOUNTS.md) (members' phones).

---

## 1. The two rules

1. **HomeBrain operates no servers.** Not a tunnel, not a relay, not an
   account service, not DNS.
2. **No third-party server can decipher the traffic.** Any machine between a
   phone and the box that the owner does not physically hold sees ciphertext
   only. Encryption ends on the phone and on the box.

Rule 2 is stricter than "we trust the provider." A provider that terminates
TLS fails it, however reputable.

Reaching a box behind a home router needs a rendezvous somewhere; that is
physics (August review §A3). These rules do not forbid a rendezvous. They say
whose it is (not HomeBrain's) and what it may see (who connects and when,
never what is said).

---

## 2. How each path measures up

| Path | HomeBrain server | Third party can decipher | Verdict |
|---|---|---|---|
| Factory Pangolin (`*.homebrain.house`) | Yes | Yes — TLS ends at the Pangolin edge | Fails both. Retire. |
| Owner-run Pangolin on a VPS | No | Yes — TLS ends on a machine the hosting company holds | Fails rule 2. |
| Owner's Cloudflare Tunnel | No | Yes — Cloudflare terminates TLS | Fails rule 2. |
| Plain WireGuard + port forward | No | No | Passes, but does not work behind carrier-grade NAT (Starlink). Not a product. |
| Tor onion services | No | No | Passes, but needs Orbot on every phone and is slow. Not a product. |
| **Mesh VPN, private** | No | No, with Tailnet Lock on (§3) | **Passes.** |
| **Mesh VPN public address (Funnel)** | No | No — TLS ends on the box (§3, one caveat) | **Passes with a caveat.** |
| Telegram bot | No | **Yes** — bot chats are not end-to-end encrypted | **Stated exception to rule 2.** See §7. |
| Off-site backup (rclone) | No | No — archives are encrypted before they leave | Passes. |
| Health alert email | No | Yes — the owner's mail provider reads the alert | Fails rule 2 for alert text only. See §7. |

---

## 3. How a mesh VPN works

Tailscale is the candidate. Its client is open source; its coordination
service is not something HomeBrain runs.

1. **Each device runs a client that holds its own private key.** WireGuard
   underneath. The key never leaves the device.
2. **A coordination service introduces the devices.** It hands each device
   the others' public keys and current network addresses.
3. **Devices then connect to each other directly, encrypted end to end.**
   Both sides dial outward, which gets through home routers and
   carrier-grade NAT with no port forwarding.
4. **If a direct connection fails, traffic goes through a relay** that sees
   only ciphertext.
5. **Each device gets a stable private address and a name** under the
   owner's network domain (`<name>.<tailnet>.ts.net`).

### What Tailscale's documentation says (read 2026-11-13)

- **Free plan:** "Free forever", up to 6 users, unlimited user devices, for
  non-commercial use.
- **Funnel** publishes a device on the public internet at its `ts.net`
  name. It "can only listen on ports 443, 8443, and 10000." The client on
  the device "terminates the TLS connection." "Funnel relay servers do not
  decrypt the traffic between public devices and your device." Traffic is
  "subject to non-configurable bandwidth limits" (figures not published).
  It is "available for all plans" and "currently in beta."
- **Tailnet Lock:** without it, "if Tailscale were malicious, and stealthily
  inserted new nodes into your network, then Tailscale could send or receive
  traffic to your existing nodes in plaintext." With it, new devices must be
  signed by a key the owner holds before others accept them. Available on
  the free plan.

### What that means for rule 2

- **Private mesh passes only with Tailnet Lock on.** Without it the
  coordination service is trusted not to add a device of its own. HomeBrain
  must turn the lock on at join time, not offer it as an option. The box and
  the owner's first phone are the signing devices.
- **Funnel passes on the relay.** The relay forwards an encrypted stream it
  cannot open.
- **Funnel's caveat (my inference, not from the documentation):** the public
  name lives in a DNS zone Tailscale controls. A party controlling a name
  can obtain a certificate for it and point the name elsewhere. That would
  be an active, publicly logged attack rather than passive reading, but it
  is not excluded by design the way the private mesh with the lock is.
  State it; do not hide it.
- **What the third party does see:** which devices exist, when they connect,
  their network addresses, and for Funnel the public name being reached.
  Metadata, not content.

---

## 4. The design

One dependency, two modes.

| Service | At home (unchanged) | Away | Why that mode |
|---|---|---|---|
| Dashboard | `https://homebrain.local` | Private mesh only | Holds the master password. Never on the public internet. |
| OpenClaw chat (`/openclaw/`) | via dashboard | Private mesh only | Same session, same reason. |
| Box-to-box backup | — | Private mesh only | Two boxes, no phones involved, strongest path available. |
| Nextcloud | `https://nc-homebrain.local` | Public address | Phone app with nothing extra installed; members' photo backup. |
| Vault | `https://vault-homebrain.local` | Public address | Bitwarden app with nothing extra installed. |
| Home Assistant | `https://ha-homebrain.local` | Public address | Companion app with nothing extra installed. |

An owner who wants the strictest setting turns the public address off and
puts the mesh app on every phone. That is a switch, not a second design.

### What the owner does

1. Creates a free account with the mesh provider (Google or Apple sign-in).
2. Dashboard → Connectivity → **Remote access** → pastes one join key.
3. The box joins, turns on the lock, requests its certificate, and shows the
   addresses.
4. Installs the mesh app on their own phone and signs in. That phone becomes
   the second signing device.

### What a household member does

With the public address on: nothing new. Their sheet lists one server URL per
app, as today.

With it off: install the mesh app, accept an invitation, leave it on.

---

## 5. Open questions — settle before building

**Q1. Three ports on one name.** Funnel offers one name and ports 443, 8443
and 10000. Three apps fit exactly: one port each. `LAN_HTTPS.md` exists
because "ports are an implementation leak." The alternative is one port with
path prefixes, which Nextcloud and Home Assistant handle badly. Proposal:
accept ports on the away address, hidden inside a QR code, never typed.

**Q2. One URL or two per app.** At home the name is `nc-homebrain.local`;
away it is `<name>.<tailnet>.ts.net:8443`. Home Assistant's app has separate
home and away URLs. Nextcloud and Bitwarden take one. Options: phones use
the away address everywhere (simple; home traffic then depends on the
internet being up), or the LAN resolves the away name to the box locally.
Needs a test, not an argument.

**Q3. Bandwidth.** The Funnel limit is unpublished. Phone photo and video
upload is the workload that will find it. Measure on the test box with a
real camera roll before committing.

**Q4. Beta.** Funnel is labelled beta. The private mesh is not. If Funnel
proves unreliable, the fallback is the strict setting for everyone.

**Q5. Members and the six-user cap.** Only relevant with the public address
off. Six covers most households; state the number.

**Q6. Plan terms.** The free plan is for non-commercial use. An owner using
their own account at home fits. HomeBrain must not sign owners up, hold
their keys, or resell the service.

**Q7. One VPN at a time.** A phone runs a single VPN. A member with a work
VPN cannot use the strict setting. Another reason the public address is the
default for apps.

**Q8. Trusted proxies.** Nextcloud, Home Assistant and Vaultwarden each have
a list of proxies and hostnames they accept (`configure_nc_ha_proxy_settings`
in `common.sh`). A new name and a new front door are a third copy of that
fact. One owner per fact (August review §C2).

---

## 6. Build order

1. **Prove it by hand on the test box.** Join, lock, Funnel three ports,
   connect all three phone apps from mobile data, upload a camera roll,
   replicate a backup between two boxes over the mesh. Answers Q1–Q4. No
   code until this passes.
2. **Join from the dashboard.** Host-level service, not a container.
   `detect_platform` covers ARM and x86. Self-test row with three outcomes.
3. **Lock on by default,** with the owner's phone as second signer and a
   recovery path written into `DISASTER_RECOVERY.md`. A lost signing key
   must not strand the box.
4. **Public address switch** and the proxy/hostname settings behind it.
5. **Onboarding sheet and QR** for owner and member phones.
6. **Replica card** shows the mesh address.
7. **Migrate `berlin` and `miami`,** then retire the factory tunnel,
   registrar and Cloud Account card (review §5 S6).
8. **Demote Cloudflare and owner-run Pangolin.** Both fail rule 2. Either
   remove them or label them "the provider can read this traffic."

---

## 7. Decisions rule 2 forces elsewhere

These are the owner's to make. They are outside the tunnel but inside the
rule.

**Telegram — decided 2026-11-14: stated exception.** Telegram stays the
default chat. The README, the AI card and the Messaging Channels card now
say that Telegram can read a bot chat. Rule 2 reads "…except the messenger
you choose to link." The reasoning that was on the table:

Bot chats are readable by Telegram's servers: prompts, replies, camera
stills, delivered files. Under rule 2 as written, Telegram fails. Two honest
positions:

- **Exception, stated.** Telegram stays the default chat because it needs no
  app and pushes. The README and the Messaging Channels card say Telegram
  can read it. Rule 2 then reads "…except the messenger you choose to link."
- **Opt-in.** Once the mesh exists, the box's own chat at `/openclaw/` is
  reachable from anywhere over a path that passes rule 2. It becomes the
  default; Telegram is offered with the warning. The cost is push: nothing
  wakes the phone when the agent has something to say, and alerts, watcher
  pings and media delivery all assume a messenger today.

The owner chose the first. The box's own chat is reachable over the private
network anyway (`https://<name>:4443`, then Chat), so the second stays open
without more work.

**Health alert email.** The alert text passes through the owner's mail
provider. Low sensitivity ("backup failed"). Keep, and keep the text free of
anything personal.

**The dashboard sentence.** Fixed: "Inference never leaves the device. Chat
goes through the messenger you link below."

---

## 8. Non-goals

- A HomeBrain-run relay, coordination server, or DNS.
- A self-hosted coordination server as the default. It needs a public
  machine, which is a server the owner runs. Fine for experts, unsupported.
- A native HomeBrain mobile app.
- Plain WireGuard with port forwarding.
- Path-prefix hosting of Nextcloud or Home Assistant.

---

## 9. What was built (2026-11-14)

Code: `src/mesh.py` (the only place that runs the client), routes under
`/api/mesh/` in `src/app.py`, `ensure_tailscale` in `scripts/common.sh`, the
**Remote access** card on the Connectivity tab.

### Differences from the proposal

- **Sign-in link, not a pasted key.** §4 said "pastes one join key." The
  client offers a sign-in link with no key at all, so the owner clicks
  **Connect**, opens the link, and signs in. HomeBrain never holds a
  credential for the owner's account.
- **Three explicit steps, not one.** Connect, then **Turn on remote access**,
  then optionally **Give the phone apps a public address**. Turning it on
  restarts three services and changes every away address, so it does not
  happen as a side effect of signing in. The public address is off until
  asked for: it puts three login pages on the internet.
- **The lock is a button, not automatic.** §3 said HomeBrain must turn it on
  at join time. It is tailnet-wide: it changes how every device on the
  owner's existing network is admitted, and it produces secrets that must be
  written down. The card shows "The lock is off" as a warning until it is
  on, and says what that means. Rule 2 is not met for the private path until
  the owner presses it. This is the one place the build is weaker than the
  rule, on purpose; revisit after the hardware walk shows what init does to a
  network that already has devices.
- **No support secret.** `lock init` is run without
  `--gen-disablement-for-support`. That flag gives Tailscale a secret that
  turns the lock off.
- **A third remote provider, not a new mode.** Turning it on writes the same
  keys the tunnel writes (`DEPLOYMENT_MODE`, `NEXTCLOUD_TRUSTED_DOMAINS`,
  `HA_TRUSTED_DOMAINS`, `VAULT_DOMAIN`, …) plus `MESH_DOMAIN`. Trusted
  domains, the vault URL, pairing links and the replica URL follow with no
  second copy of those facts (Q8). One provider at a time: turning this on
  clears tunnel credentials from `.env`, and turning a tunnel on clears this.

### The addresses

| Service | Address | Public address allowed |
|---|---|---|
| Nextcloud | `https://<name>` | Yes |
| Vault | `https://<name>:8443` | Yes |
| Home Assistant | `https://<name>:10000` | Yes |
| Dashboard and chat | `https://<name>:4443` | **Never** |

A public address can only use 443, 8443 and 10000. Three apps take all
three, so the dashboard sits on a port that cannot be made public. That is
structural, and `test_mesh.py` pins it.

### Open questions, answered or not

- **Q1 (ports).** Accepted as proposed.
- **Q2 (one URL or two).** Built as one: the away address is the app's
  address, as it is with a tunnel today. Cost: with the public address on, a
  phone at home also goes out through the relay. Not measured.
- **Q3, Q4 (bandwidth, beta).** Open. Need the signed-in walk.
- **Q8 (proxies).** No new proxy entry needed: the client proxies from the
  host, so requests arrive from the Docker bridge like Caddy's do. From the
  pinned source (`ipn/ipnlocal/serve.go`), it keeps the original `Host` and
  sets `X-Forwarded-Host`, `X-Forwarded-Proto` and `X-Forwarded-For`. Read,
  not observed.
- **New: Vault attachments.** Vaultwarden builds attachment links from its
  one configured address, which becomes the away address. On the home
  network by `.local` name, attachment downloads may point at the away name.
  Same as a tunnel box today. Unchecked.
- **New: lost box with the lock on.** See
  [`DISASTER_RECOVERY.md`](../DISASTER_RECOVERY.md#remote-access-after-a-restore).
  The backup does not carry the box's network key. Carrying it would let a
  restore resume as the same device and is the better answer; not built.

### Not built

- Retiring the factory tunnel, registrar and Cloud Account card (§6 step 7).
  Both production boxes still depend on them.
- Demoting or labelling Cloudflare and owner-run Pangolin in the dashboard
  (§6 step 8). The README now says both decrypt off the box.
- A phone onboarding sheet for the private network (§6 step 5).
- Any change to the setup wizard. Remote access is turned on after setup.

