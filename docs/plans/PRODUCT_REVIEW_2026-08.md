# Product & engineering review — August 2026

**Status:** open. Findings below are the next work list; nothing here has been
acted on except documenting them.
**Date:** 2026-08-12
**Predecessor:** [`PRODUCT_REVIEW_2026-07.md`](PRODUCT_REVIEW_2026-07.md)
(Phases 1–5 shipped). This is not a re-litigation of July. It is a senior
read of the tree after that work landed, plus an answer to whether HomeBrain
is more than the stack it deploys.
**Essence:** *Your private cloud and agent. Nothing leaves the box.*

---

## TL;DR

HomeBrain is an **appliance**, not a web app and not a compose file. The
product is four independent systems behaving like one box a non-SSH owner can
trust. That glue is real value. The thing that will hurt you is not missing
features. It is (a) the agent-is-root + email/web ingestion combination, which
is load-bearing and cannot be patched around, and (b) god files that keep two
copies of the same fact. Both are known. The first is a product stance. The
second is the engineering debt that keeps producing “it said it worked” bugs.

ARGB fan lighting can wait.

---

## 1. What this actually is

You are gluing four mature products (Nextcloud, Home Assistant, Vaultwarden,
OpenClaw) into a single owner-operated machine, then putting a local 35B agent
in front of them so the owner never needs a shell. The Flask dashboard is the
control plane. Telegram pairing is the front door. The GPU is the
differentiator; without it you still ship a private cloud (HomeCloud).

That bet is coherent. Most self-hosted stacks dump a compose file and a wiki
on the user. This one takes the last mile seriously: wizard, factory password,
generated secrets shown once, backup that is supposed to restore, updates that
refuse to go backwards, a nuclear reset that returns the box to the wizard.

The July review got executed rather than filed. That is rare, and it shows:
CI, encrypted+off-site backups, the notifier, `unattended-upgrades`, the
agent trust boundary made explicit, self-test, household members, Nextcloud
data on a second drive. The remaining work is a shorter list and a sharper
one.

---

## 2. Value-add vs the stack it deploys

Anyone can `docker compose up` Nextcloud, Home Assistant, Vaultwarden, and
OpenClaw. What you then have is four admin passwords, four backup stories,
four upgrade landmines, and an agent that does not actually know the house.
HomeBrain’s value is the glue those projects will never ship, because each of
them is *a* product, not *the* box.

**What is real, not packaging**

- **One secret, one recovery.** A master password that is the dashboard,
  Nextcloud, Home Assistant, and the backup encryption key, plus a phrase that
  resets the lot without leaving HA and `.env` disagreeing. That
  rotation/restore path is months of incident-driven work. A stack does not
  have it.
- **A backup that restores the composition.** Encrypted archive, off-site copy
  that does not delete itself, secrets that still decrypt, a dashboard restore,
  a documented bare-metal path. `docker compose` will not save you when the
  disk dies. This is the main reason the project exists.
- **An agent that is wired, not adjacent.** MCP with Read/Act/Reveal, consent
  in chat, audit logs, credentials derived rather than pasted into JSON,
  household members who get files without getting the house. OpenClaw on its
  own is a chatbot with a shell. Here it is the operator.
- **Updates that refuse to brick you.** Pins, a downgrade guard (Nextcloud
  migrations are one-way), a wizard, a nuclear reset back to factory. Those
  are appliance problems. The upstream projects do not care about your box as
  a whole.
- **The GPU path as a device, not a wiki.** Vulkan on consumer AMD, platform
  detection, always-on, models you can actually select. That is not
  `apt install llama.cpp`.

**What is not extra value**

The container list, Caddy, the tunnel, a status dashboard. Umbrel and Yunohost
already do “click to run Nextcloud.” If HomeBrain were only that, there would
be no project.

**The honest test.** Would a careful sysadmin who already runs this stack
still want it? For backup, identity, and the agent wiring — yes, or they will
reinvent it badly. For “I like editing YAML” — no. For the person the README
is written to — the stack without this glue is not a product.

---

## 3. What's unusually good

Keep doing these. They are the reason the glue works.

**Honesty about privilege.** The agent is root-equivalent (`docker` group +
NOPASSWD sudo + `tools.exec.mode=full`). That is the correct model for “the
owner never needs a shell.” Pretending a path deny-list contains a process
that can `docker run -v /:/host` would be worse. The security boundary is
Telegram `allowFrom` plus a loopback-bound gateway.

**Incident-driven tests.** The suite is not coverage theater. Each file exists
because something broke on hardware: Fernet keys truncated on restore, HA
rotation reporting success while writing nothing, off-site `rclone sync`
deleting the remote copy, internal-storage restore dying on a mount guard, a
cron tool schema killing every agent turn. CI now gates those.

**“Prove the claim.”** `src/selftest.py` is the best idea in the repo. Three
outcomes, never two — `skip` must not render as pass. `scripts/healthcheck.py`
notifies on *transitions*, with no LLM in the path, so alerts still arrive
when llama-server is down.

**Pinning and two-profile compose.** Images, llama.cpp, OpenClaw, Proton
Bridge are pinned. Profiles keep optional surfaces off until configured.
Platform detection is one function in `common.sh` writing `.platform.json` —
bash and Python used to disagree about what a GPU is.

**Recovery as a product.** Word-based master password, independent
scrypt-hashed recovery phrase shown once, LAN-only by default, rotation that
re-credentials services *before* rewriting `.env`, post-rotation backup so
you are not locked out of archives.

**Minimalism that is real.** No frontend framework, no CDN, no Prometheus.
The dashboard is HTML + one CSS + one JS file and it works with the WAN
unplugged. When JS was extracted from the 3.5k-line template, they extracted
JS — they did not introduce React.

**The extraction pattern, when used.** `recovery.py`, `selftest.py`,
`integrations.py`, `mcp_common.py`. Do not rewrite `app.py`. Extract the
*next* feature into a module, the way they already did.

---

## 4. Findings to act on

Verified against the working tree on 2026-08-12. Ordered by blast radius, not
by how interesting they are to implement.

### A. Trust — do these first

**A1. Disclosed credentials are still live.**
[`DISCLOSED_CREDENTIALS.md`](../DISCLOSED_CREDENTIALS.md) is **open**. Recovery
phrase, master password, HA token valid into 2036, Pangolin secrets, SSH
`admin`/`admin` — treated as known to a model provider, not rotated. Moving
`misc` into Vaultwarden does not change that; only new values do. This is more
urgent than any feature on the roadmap. Follow that doc’s order. Do not
re-create `misc` with the new values.

**A2. `AGENTS.md` is behavioral only — by design.**
It is injected into every agent prompt, so it stays short and has no
repo internals (those go stale and tax every session). The hardware-test
lock lives in [`TESTING.md`](../TESTING.md) pre-flight. The agent-is-root
trust boundary lives in this file (§B1) and in the comments in
`utilities.sh` next to `tools.exec` / `tools.elevated`. Do not put them
back into `AGENTS.md`. #172: `tools.elevated.enabled` must be true or
sudo looks broken after upgrade; `allowFrom` stays unset.

**A3. Do not ship a cloud dead-man's switch.** Closed as a product
decision, not as unfinished work. A watchdog that notices a *dead* box
has to live off the box — that is physics. The design was a POST of
`device_id` + health to a Cloudflare Worker every few hours. That is a
HomeBrain cloud: someone else's server, a cloud account, and a liveness
feed. It contradicts *nothing leaves the box* and the README's "no one
else's servers."

Silence when the box is dead is the price of that stance. The owner
notices when Telegram stops answering or the dashboard does not load.
Disaster recovery is the off-site backup (encrypted, destination the
owner chose) — not a ping to us. The device half stays behind
`HEARTBEAT_ENABLED` (default off). Do not deploy `registrar/heartbeat.js`.
A user who wants a watchdog can point `HEARTBEAT_URL` at an endpoint they
run; we will not operate one.

**A4. Bare-metal off-site restore onto replacement hardware — walked 2026-08-12.**
On `homebraintest.local` (RPi4, no backup drive, `BACKUP_INTERNAL` was not
set). Canary file in Nextcloud → full backup → off-site SFTP →
`nuclear_reset` → wizard **Restore system** (not the two-pass dashboard
path) → `ensure_staging_dir` logged *No backup drive at /mnt/backup —
staging the off-site archive on the internal disk* → fetch 63 MB →
decrypt → unpack → canary present, dashboard login with the pre-reset
master password, all six containers healthy.

Honesty notes, same class as TIER1:
- The SFTP remote was `127.0.0.1:/home/admin/offsite-e2e` (survives the
  wipe). Software path proven. Not a WAN transfer of an 80 GB archive.
- `HOMEBRAIN_EMAIL_KEY` in the archive was already 43 characters (padding
  lost on an earlier restore of this box). This run did not shorten it
  further. Dashboard re-pads on read; `.env` on disk is still 43.
- Wizard restore does not set `BACKUP_INTERNAL=true`. After this run the
  box would have looked for `/mnt/backup` on the next backup. Set by
  hand afterwards. The wizard should set it when it stages internally.

The runbook in [`DISASTER_RECOVERY.md`](../DISASTER_RECOVERY.md) still
described a two-pass install-then-overwrite; the wizard restore door is
the path that was walked.

### B. Security — product stance, then hardening

**B1. Inbound agent content is the threat model.**
A prompt-injected email or a browsed page can reach a root shell. MCP consent
(Read / Act / Reveal) and audit logs bind *tool* calls; they do not bind
`exec`. Do not try to sandbox the agent. Treat channel pairing and anything
that widens what the agent will read as privilege escalation. Email-to-agent
is the scariest surface on the box; body fetch should stay default-off.
Household members (#157) getting Nextcloud without the house keys is the
right split — there is still one agent, and it is the owner.

**B2. Dashboard is still gunicorn as root on `0.0.0.0:80`.**
Vault and Nextcloud got Caddy; the service that holds the master password did
not. July deferred this because both prod boxes are remote (Pangolin TLS).
That deferral still holds **if** LAN-only is not a real user. The README
should say so plainly: remote mode has TLS; local mode does not, on the one
service that matters most. Revisit the Caddy edge when a LAN-only deployment
is real — the bootstrap-without-containers problem is unchanged.

**B3. No container hardening.**
No `cap_drop`, `no-new-privileges`, or memory limits anywhere in
`docker-compose.yml`. `.env` is `root:root 0600` specifically so a container
escape landing as `www-data` cannot read it. The compose file does not hold
up its half of that bargain. Cheap, and it matches the invariant already
written down.

**B4. `INTEGRATIONS_PLAN.md` still talks WhatsApp as the primary surface.**
WhatsApp was deleted. Stale plan docs are bad prompts. Either mark the plan
historical and point at current behaviour (Telegram-only, stock OpenClaw),
or rewrite the opening so an agent does not re-introduce a channel.

### C. Engineering — the dual-source bug class

The glue *is* the product, and the glue lives in a handful of god files
(line counts as of 2026-08-12):

| File | Lines | Role |
|---|---|---|
| `src/app.py` | ~5,180 | 89 routes: wizard, auth, drives, backup, AI, household, updates |
| `src/static/dashboard.js` | ~3,030 | The actual UI |
| `scripts/utilities.sh` | ~2,580 | OpenClaw config, GPU stack, migrations |
| `scripts/common.sh` | ~1,690 | Platform, env, off-site, shared helpers |
| `src/integrations.py` | ~1,645 | Five MCP integrations + Telegram |

The rule “read the section you need” is a coping strategy, not a design. The
class of bug this produces is **two copies of the same fact**. Restore
required a mount that backup had already made optional. `.env` parsers split
on every `=` and ate Fernet padding. HA password lived in two places and
only one was true.

**C1. Do not rewrite `app.py`.** Extract the next slice. Backup / restore /
drives is where the dual-source bugs keep appearing, so that is the slice
to take when those files are being touched anyway. Same rule as July:
blueprints get extracted only when a file is being touched anyway — but
when it is, take the slice.

**C2. One owner per fact.** Before adding a branch to `restore.sh` that
already exists in `backup.sh`, put the helper in `common.sh`. That is what
Phase 5a did for `ensure_backup_dir()`. Apply it as a reflex, not a phase.

---

## 5. Plan

Ordered by user impact. Same minimalism guardrails as July: no frontend
framework, no Prometheus, no multi-box orchestration, no plugin system, no
`app.py` rewrite.

### Phase 0 — Hygiene (~1 hour)

Done in the same pass as this review’s README pass:
- `AGENTS.md` is behavioral only. Lock → `TESTING.md`. Trust boundary stays
  here and in `utilities.sh`, not in the always-injected prompt.
- README: value-add vs the stack; no “monitored without you”; LAN dashboard
  is HTTP, remote is HTTPS through the tunnel.

Done:
- [`INTEGRATIONS_PLAN.md`](INTEGRATIONS_PLAN.md) marked historical.
  Telegram only; WhatsApp must not come back.

### Phase 1 — Trust the tin (ops, not code)

- **A1.** Rotate every credential in `DISCLOSED_CREDENTIALS.md`, in the
  order that doc specifies. Put new values in Vaultwarden. Do not put them
  in the repo tree.
- **A3.** Closed. No HomeBrain-operated heartbeat. See §A3.
- **A4.** Walked 2026-08-12 on `homebraintest.local`. See §A4. Follow-up:
  wizard restore should set `BACKUP_INTERNAL=true` when it stages on the
  internal disk.

### Phase 2 — Hardening that matches invariants already written

- **B3.** `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and
  modest `mem_limit` on compose services that can take them. Start with
  `newt`, `redis`, `caddy`; treat Nextcloud/HA/Vault as a second pass
  because they are the ones that will surprise you.
- **B1.** Audit email MCP defaults and the browser tool. Any path that
  widens what the agent will read needs an explicit product decision, not
  a silent enable.
- **B2.** Remains deferred until a LAN-only user is real. The README
  sentence in Phase 0 is the stopgap.

### Phase 3 — Extract when touching, not before

- Next time backup / restore / drives are in scope, lift them out of
  `app.py` into a module. No dedicated “refactor week.”
- Same for any new OpenClaw config key: `utilities.sh` is the owner.
  Do not add the key to `AGENTS.md`.

---

## 6. Deliberately not on the list

- ARGB AI feedback lighting.
- Rewriting `app.py` / `dashboard.js` / `utilities.sh` for sport.
- Sandboxing the agent with a filesystem deny-list.
- Configuring `tools.elevated.allowFrom` as a second sender gate.
- Caddy in front of the dashboard before a LAN-only user exists.
- A HomeBrain-operated dead-man's switch (Cloudflare Worker heartbeat).
  Silence when the box is dead is the price of nothing leaving it. See §A3.

---

## 7. Bottom line

This is a serious appliance built by someone who got burned by silent success
and then systematically removed that class of lie. The AI stack on consumer
AMD hardware is a real differentiator. The recovery, backup, and update
machinery is more grown-up than most commercial NAS software.

I would run this on my own hardware, with Telegram pairing treated like a
root password, and I would not hand the agent an inbox until I was
comfortable with that. I would not sell it as “the AI is sandboxed.” The
repo already refuses to. That refusal is the most senior thing in it — and
it only works if the people (and agents) changing `tools.exec` still read
the invariant, which now lives next to the code, not in every prompt.
