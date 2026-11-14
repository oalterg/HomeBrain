# Product review — November 2026: the way in from outside

**Status:** partly acted on (2026-11-14). S1, S2 and the claim fix in §6 are
in tree; S3–S7 are open. State of the build:
[`REMOTE_ACCESS.md`](REMOTE_ACCESS.md) §9.
**Date:** 2026-11-13
**Predecessor:** [`PRODUCT_REVIEW_2026-08.md`](PRODUCT_REVIEW_2026-08.md).
**Scope:** one question — what is the obvious missing product element, given
the goal that **HomeBrain operates no servers, Pangolin included**. Read for
this: `README.md`, `ROADMAP.md`, both earlier reviews, the plan docs,
`PANGOLIN.md`, the dashboard template, `registrar/`, and the call sites named
below. Not a code audit. No option in §4 has been tested on a box.
**Essence:** Take away the servers HomeBrain runs and the agent still has a
way out of the house (Telegram). The apps do not. That is the gap.

---

## TL;DR

Everything that reaches the box from outside today goes through one of two
rendezvous points. Chat goes through Telegram, which someone else runs.
Files, vault, Home Assistant, the dashboard and box-to-box backup go through
a Pangolin server, which HomeBrain runs. Remove the second and nothing
replaces it.

The missing element is **remote access that needs no HomeBrain server and no
owner-run server**: a mesh VPN the owner joins with their own account, set
up from the dashboard, with the same one-QR phone onboarding the LAN already
has.

An earlier draft of this review named Telegram as the problem. Under this
goal that was backwards. Telegram is the pattern to copy: a rendezvous
operated by a third party, on the owner's own account, at no cost to
HomeBrain. §6 keeps the one part of that draft that still holds.

---

## 1. What HomeBrain operates today

| Server-side piece | Where it shows in the tree | What depends on it |
|---|---|---|
| Pangolin server (`*.homebrain.house`) | `newt` container, `PANGOLIN_ENDPOINT` / `NEWT_ID` in `factory_config.txt`, `docs/PANGOLIN.md` | Every public URL: dashboard, `nc.`, `ha.`, `vault.` |
| Pangolin resource map | `PANGOLIN.md` "Pangolin side (manual)" | Four hand-made resources per box |
| Registrar Worker + Zitadel | `registrar/worker.js`, `REGISTRAR_URL` / `REGISTRAR_SECRET`, `/api/cloud/register`, the "Cloud Account" card with a subscription tier | Accounts for the tunnel's sign-in |
| Heartbeat Worker | `registrar/heartbeat.js` | Nothing — declined in August §A3, never deployed |

GitHub serves the installer and updates. That is someone else's server, not
one HomeBrain operates, and is out of scope here.

The August review turned down the heartbeat Worker because a liveness ping to
a HomeBrain-run endpoint contradicts "no one else's servers." The Pangolin
server is the same thing at a much larger scale: it terminates TLS for every
tunnelled box, so the operator can read the traffic. The goal in this review
is that decision applied consistently.

---

## 2. What breaks when those go away

**HomeCloud has no remote path at all.** `README.md`: "Without a GPU, a
Pangolin tunnel is how you reach the browser from outside." No GPU means no
agent and no Telegram. A Raspberry Pi HomeCloud becomes a LAN-only device.

**The phone apps stop working away from home.** The product puts three apps
on the owner's phone and on each household member's phone: Nextcloud (files,
photo backup), Bitwarden (vault), and the Home Assistant companion. Telegram
replaces none of them. A password manager that only syncs at home and photo
backup that waits until the phone is back on the Wi-Fi are the visible
losses.

**Household members lose everything outside the house.** Members have no
agent and no dashboard (`HOUSEHOLD_ACCOUNTS.md`). Their whole product is
those three apps.

**Box-to-box backup has no route.** `README.md` offers "a second HomeBrain,
which hands out a dedicated `replica` account" as an off-site target. The
sender needs a URL for the receiver. Between two homes that URL is a tunnel
hostname today.

**What does not break.** On a GPU box the agent still answers on Telegram,
sends camera stills, delivers generated media, and passes documents up to
20 MB. The health notifier still pushes, with email as fallback. The LAN is
untouched: Caddy, `.local` names, HTTPS on 443.

---

## 3. What already exists, and why it is not enough

- **Bring your own Pangolin** (#256, "Set up your own tunnel"). Removes
  HomeBrain's server by asking the owner to run one: a VPS, a domain, and
  four resources mapped by hand. That is an expert path, not a product.
- **Bring your own Cloudflare Tunnel.** Needs a domain and a Cloudflare
  account, covers Nextcloud and Home Assistant profiles only, and
  `src/app.py:1926` describes cloudflared as "on its way out." Cloudflare
  also terminates TLS, so a third party can read the traffic.
- **Telegram.** The right shape — third-party rendezvous, owner's account —
  but it carries chat, not app protocols.

So the building blocks for "no HomeBrain server" exist only for owners who
can run infrastructure themselves.

---

## 4. The missing element

A remote path with these properties:

1. HomeBrain operates nothing.
2. The owner operates nothing beyond an account.
3. No port forwarding, and it works behind carrier-grade NAT. One of the
   project's own boxes sits behind Starlink.
4. The rendezvous cannot read the traffic.
5. The three phone apps work unchanged, for the owner and for members.
6. Two boxes can reach each other for backup.

Reaching a box behind NAT needs a rendezvous somewhere; August §A3 called
this physics. The only choice is whose it is.

### Options

| Option | Whose rendezvous | CGNAT | Reads traffic | Phone apps | Cost to the owner |
|---|---|---|---|---|---|
| Mesh VPN (Tailscale, or a self-hosted control plane for experts) | Third party, owner's account | Yes | No — WireGuard end to end; relays see ciphertext | Yes, with the VPN app on | One more app and account per phone |
| Cloudflare Tunnel, owner's | Third party, owner's account | Yes | Yes | Yes, no extra app | A domain |
| Plain WireGuard to the home router | None | **No** | No | Yes | Port forward and a dynamic DNS name |
| Tor onion services | The Tor network | Yes | No | Poorly — needs Orbot, slow | High friction |
| Owner-run Pangolin (today) | The owner's VPS | Yes | Owner only | Yes | Running a server |

### Recommendation

**Mesh VPN as the built-in remote path**, with the owner's own account.

- It is the only row that meets all six properties.
- It mirrors the Telegram decision exactly: a free third-party service on
  the owner's account, HomeBrain operating nothing.
- It solves box-to-box backup with no extra work: both boxes join the same
  network and the replica URL is a private address.
- It keeps the dashboard off the public internet. Today a tunnelled box
  exposes the page that holds the master password to anyone with the URL.

What it costs, stated plainly:

- Each phone needs the VPN app installed and switched on. That is worse
  than a public URL for household members and is the main product risk.
- A third party sees metadata: which devices exist and when they connect.
  Not content. Write this down the way §A3 was.
- The README's "no cloud accounts" becomes "no HomeBrain account." It
  already has that exception for Telegram without saying so.

Do not build plain WireGuard: it fails on exactly the connections this
product meets.

**Added 2026-11-13 — second rule and the design.** The owner added a
constraint: no third-party server may be able to decipher the traffic. That
rules out Cloudflare Tunnel and owner-run Pangolin on a VPS (both terminate
TLS off the box), and it puts Telegram in question. The design that meets
both rules, including a public address for the phone apps whose encryption
ends on the box, is in [`REMOTE_ACCESS.md`](REMOTE_ACCESS.md). Where this
review and that plan differ, the plan wins.

---

## 5. Suggestions, in order

**S1. Decide the remote story and write it down (an hour).** One paragraph
in `README.md` and `ROADMAP.md`: HomeBrain operates no servers; chat leaves
the house through Telegram; apps leave through a private network on the
owner's account. Everything below follows from that sentence.

**S2. Ship the mesh join as a dashboard feature (the real work).**
Connectivity gets a third choice beside Pangolin and Cloudflare: paste one
key, the box joins. Host-level, not a container, so it survives a compose
restart. Self-test gains a row that proves the box is reachable on it, with
the usual three outcomes.

**S3. Names and certificates on the private network.** The hard part.
Nextcloud and Bitwarden clients require HTTPS, and `LAN_HTTPS.md` solved
that with four `.local` names and an internal CA. `.local` does not resolve
over a VPN. The private path needs its own answer for four hostnames and a
certificate the phone trusts. Settle this before S2 is built, because it
decides whether a phone keeps one server URL or needs a second.

**S4. Onboarding for a phone, including a member's.** One sheet or QR that
covers: install the VPN app, join, then the existing Nextcloud / Bitwarden /
HA steps. A member must be able to do it without the owner's account
password. The phone-photo QR is the pattern.

**S5. Box-to-box backup over the same network.** The replica card shows the
private URL. `DISASTER_RECOVERY.md` gains the case where the receiver is a
second HomeBrain with no public name.

**S6. Retire what HomeBrain runs.** In this order, after S2–S5 work on both
production boxes:

- migrate `berlin` and `miami` off `*.homebrain.house`, including the
  `miami → berlin` replication that depends on those names;
- remove the factory tunnel path from `provision.sh` and the "revert to
  factory" button, or repoint "factory" to mean local-only;
- remove the Cloud Account card, `/api/cloud/register`, and `REGISTRAR_*`;
- delete `registrar/worker.js` and `registrar/heartbeat.js`;
- rewrite `PANGOLIN.md` as an owner-run option only;
- shut down the Pangolin server, Zitadel, and the Worker. This also closes
  three rows of `DISCLOSED_CREDENTIALS.md` by removing what they protect.

**S7. HomeCloud's alerts.** Without a GPU there is no Telegram, and without
a tunnel the only remote signal is the health email. Confirm that path works
on a box with no tunnel and no agent, and say in the README that it needs a
mailbox.

### Not suggested

- A HomeBrain-run relay, coordination server, or DNS. Same objection as the
  heartbeat Worker.
- Removing the owner-run Pangolin and Cloudflare options.
- A native HomeBrain mobile app.
- Replacing Telegram.

---

## 6. Still true from the first draft

One line in the product overstates what it does.
`src/templates/dashboard.html:180` says "Inference and chat never leave the
device." Inference does not. Chat does: Telegram bot conversations are not
end-to-end encrypted, so Telegram's servers can read prompts, replies,
camera stills and delivered files.

That is an accepted trade under this goal, not a defect. It should be said
once, where the owner links the bot, and the dashboard sentence should
become "Inference never leaves the device." The README's "No cloud accounts"
wants the same honesty once S1 is written.

---

## 7. Smaller gaps, unchanged

- **Only the owner can talk to the agent.** Deliberate while the agent is
  root-equivalent. A member-facing agent would be a separate, unprivileged
  one. Worth a plan doc before anyone asks.
- **No voice in the house.** `whisper-server` hears; nothing speaks, and
  nothing is wired into Home Assistant's Assist pipeline.

---

## 8. Bottom line

The agent already leaves the house without HomeBrain running anything. The
rest of the product leaves through a server HomeBrain runs. Give the apps
the same deal the agent has, then switch the servers off.
