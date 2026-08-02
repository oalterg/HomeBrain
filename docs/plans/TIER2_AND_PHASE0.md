# Tier 2 + Phase 0 — Handoff for the next agent session

**Status:** Not started (2026-08-02). Written to be picked up cold, in a session that has *not*
seen the conversation this came out of.
**Date:** 2026-08-02
**Companion doc:** [`TIER1_PROVE_IT.md`](TIER1_PROVE_IT.md) — Tier 1 is in flight in a separate
session. Nothing here depends on it. Nothing here should touch `restore.sh`, `provision.sh`,
`mcp_common.py`, `mcp-email.py`, `mcp-homeassistant.py`, `mcp-nextcloud.py`, or
`src/integrations.py`'s crypto helpers — Tier 1 is editing those.

**Read `AGENTS.md` first.** Real-hardware E2E is the merge gate for anything touching provisioning
or services. Test box: `homebraintest.local` (192.168.178.51, `admin`/`admin`).

---

## Do Phase 0 first. It is an hour, and it makes everything after it cheaper.

Stale docs are a bad prompt injected into every future agent session. Three of these are actively
misleading right now.

### 0.1 — `AGENTS.md:99` is wrong by 2.5×
```
src/               Flask app (app.py ~1750 lines), migration.py, templates/
```
`src/app.py` is **4,427** lines; `src/integrations.py` is **1,616**. An agent that trusts the
comment will happily read the "whole file" and get a third of it. Fix the number, and consider
dropping the count entirely rather than committing to keeping it accurate.

### 0.2 — `docs/ROADMAP.md` lists three shipped features as unfinished
All three are merged to `main` and live on both production boxes; the branch names cited no longer
exist:

| Line | Claim | Reality |
|------|-------|---------|
| ~35 | **HomeBrain Vault** *(in progress, `vault-integration` branch)* | Shipped. Vaultwarden is in the compose stack, backed up, tunnelled. |
| ~41 | **OpenClaw integrations** *(in progress, `vault-integration` branch)* | Shipped. Five MCP servers verified live 2026-05-05. |
| ~39 | **Hardware agnosticism** *(implemented, `feat/hardware-agnostic` branch — pending hardware verification)* | Shipped and merged (#141). |

Move them to the shipped section with their release versions. While in there: the
master-password recovery-phrase entry still says "RPi E2E outstanding" — that E2E ran on
2026-08-01 (#145), so update it.

### 0.3 — No log rotation
`/var/log/homebrain/` grows forever: `main_setup.log` accumulates across *every* provision run
(this actively broke a log-polling check during the #145 E2E), plus one audit log per MCP server.
Ship a `logrotate` config with the package and install it from `provision.sh`. Weekly, keep 4,
compress, `copytruncate` — the MCP servers hold their audit files open.

### 0.4 — No `.github/dependabot.yml`
The stack is pinned in `docker-compose.yml`, `requirements.txt`, and the llama.cpp build pin.
Version bumps are currently a manual campaign (see PRs #36–#42, #126–#133). Dependabot on
`github-actions`, `pip`, and `docker` ecosystems, monthly, grouped.

**Careful:** updates are one-way on this product — a downgrade breaks Nextcloud and the manager,
and `update.sh` has a guard that blocks them. Dependabot PRs are *proposals to test on hardware*,
never auto-merge. Say so in the PR template if one gets added.

### 0.5 — CI never imports the app
`.github/workflows/ci.yml` runs `compileall`, which catches syntax errors and nothing else.
Module-level breakage — a bad import, a decorator that throws, a missing constant — ships. Add a
smoke test that imports `app` and `integrations` with a temp `INSTALL_DIR`, and asserts the Flask
app has routes. This is the cheapest test in the repo and `app.py` and `integrations.py` currently
have **zero**.

---

## Tier 2 — limits real deployments have already hit

Ranked. Items 1 and 2 are a pair and should probably ship together.

### 1. Move Nextcloud data to another drive *(M)*

**The evidence:** miami hit **426 GB** of camera footage on the root disk. The resolution, in
2026-07, was for the user to *delete the footage*. That is a product failing, not a user error.

Drive Management can already format and mount a second drive. What it cannot do is migrate an
existing Nextcloud data directory onto it. Today that means: maintenance mode, `rsync`, edit
`config.php`'s `datadirectory`, fix the bind mount in `docker-compose.yml`, re-scan.

**Shape:** a "Move data to…" action on the Drive Management card. Pre-flight (free space, target
is not removable, target is not the root disk), maintenance mode on, `rsync -aHAX --info=progress2`
with output streamed to the existing log view, verify, flip `config.php` and the compose bind
mount, `occ files:scan --all`, maintenance mode off. Every step must be resumable — this moves
hundreds of gigabytes and *will* be interrupted.

**Trap, from prior E2E:** the Nextcloud bind mount is the known sharp edge on this box (see the
clean-provision E2E notes — the "NC bind-mount trap"). Changing it wrongly loses data silently.
Snapshot `config.php` before touching it, and never delete the source until a post-move
`files:scan` succeeds.

**Done when:** on the test box, with a USB drive attached, moving a populated data dir and then
uploading a file lands it on the new drive, surviving a reboot.

### 2. Phone photo backup as a first-class feature *(M — the product bet)*

**The evidence:** nothing in the repo manages Nextcloud apps — `occ app:enable` appears **nowhere**.
The photo story is whatever stock Nextcloud gives you, discovered by the user unaided.

For the target owner, replacing Google Photos / iCloud is *the* reason to own this hardware. It is
the strongest consumer hook the box already supports, and it pairs directly with item 1 — photos
are what fills the drive.

**Shape:** a dashboard card that enables and configures the relevant Nextcloud apps (`photos`,
`memories`, `previews`), sets sane preview generation for the box's actual CPU/GPU, and — the part
that matters — renders a **QR code that sets up the Nextcloud mobile app's auto-upload in one
scan**. Nextcloud has a login-flow v2 endpoint that produces exactly this; use it rather than
asking the user to type a URL and password on a phone.

**Non-goal:** do not write a photo UI. Nextcloud has one. This is enablement and onboarding.

**Done when:** on hardware, a phone scans the code and photos appear in Nextcloud without the user
typing anything.

### 3. Household members *(M — needs a product decision before design)*

**Do not start building this without asking the user first.** One master password for everything is
the current design, deliberately. But a house has more than one person, and today adding a family
member means visiting three separate UIs (Nextcloud, HA, Vaultwarden) with three separate concepts
of identity.

The open question is direction, not implementation: is a "household member" a real account in each
service, provisioned in lockstep from one dashboard form? Or is HomeBrain single-admin forever, with
other people simply being Nextcloud users the admin creates? Those lead to very different products.
Bring the user the trade-off; do not pick for them.

---

## Deliberately not doing

No frontend framework. No Prometheus/Grafana. No multi-box orchestration. No `app.py` rewrite. The
minimalism guardrails in `PRODUCT_REVIEW_2026-07.md` still hold — if a change needs a new runtime
dependency to justify itself, it is the wrong change.

---

## One security item, not a feature

The gitignored `misc` file holds **live production credentials**, including a Home Assistant
long-lived token valid into 2036 and the registrar secret. It has been read into agent sessions
repeatedly. The July product review already flagged this.

Those credentials should be treated as **disclosed and rotated** — not merely moved into the Vault.
This needs the user to act; an agent cannot rotate the registrar secret alone. Raise it, do not
quietly file it.
