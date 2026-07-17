# Product review & improvement plan — July 2026

**Status:** Phase 2 complete (#115 #117 #118 + replica target). Phase 3 largely complete;
Caddy LAN-HTTPS edge deferred — both production boxes are remote-mode (Pangolin
provides end-to-end TLS) and the edge would entangle the pre-stack bootstrap flow
where the dashboard must run without containers. Revisit if LAN-only deployments
become a real user base. Phase 1 (CI) is the known open debt.
**Date:** 2026-07-15
**Scope:** Holistic review of the product — reliability, security, engineering foundation,
missing features — with a phased, minimalist plan. Findings verified against the working
tree at `705129a` (post-PR #112).

---

## TL;DR

The core promise ships and works: provision → wizard → Nextcloud/HA/Vault/AI agent →
backup/restore → guarded updates, verified on two real deployments. The AI stack is
genuinely differentiated (35B agent at 80K context, eviction-safe, on consumer hardware).
But the product's entire value proposition is **trust** — and that is exactly where the
three biggest gaps are: **failures are silent, backups are a single unencrypted local
copy, and the OS never patches itself.** Plus one engineering gap that taxes everything:
**zero CI**, so every regression is discovered on live hardware (PRs #106/#107/#108 were
three consecutive deploy-chain bugs found that way in one day).

## What's strong

- Recovery-phrase design (scrypt-hashed, shown once, LAN-only by default) and the
  downgrade guard are thoughtful for the target user.
- Version pinning + one-click update, profile-gated compose, rate limits on essentially
  every endpoint, atomic writes, documented security invariants.
- The instinct to add regression tests after incidents (`test_recovery.py`,
  `test_update_guard.sh`) — it just needs a runner.

## Findings

### A. Trust & reliability — highest product impact

1. **Failures are silent.** No notification path exists (no SMART monitoring, no
   alerting, nothing watches the nightly backup). A non-SSH user learns their backup has
   been failing for three months the day they need it. The perfect delivery channel
   already exists — the agent on WhatsApp/Telegram plus email — but nothing pushes to it.
2. **Backups are one unencrypted local tarball.** `backup.sh` stages everything
   (Nextcloud files in plaintext, a full Vaultwarden SQL dump) into a `tar.gz` on an
   attached drive. Stolen/failed drive, fire, or theft of box+drive = total loss or total
   exposure. No off-site option, no post-backup integrity verification.
3. **Updates don't self-protect.** `update.sh` snapshots only `docker-compose.yml` and
   `openclaw.json`; its downgrade warning says "Hope you have a backup"
   (`scripts/update.sh:48`) without taking one. Update checks are manual — a dashboard
   button hitting the GitHub API; no periodic check, no nudge.

### B. Security hardening

1. **No automatic OS security updates.** `unattended-upgrades` is never installed. An
   internet-tunneled appliance owned by people who never SSH must self-patch.
2. **The dashboard is a root process serving plain HTTP.** `homebrain-manager.service`
   runs gunicorn as root on `0.0.0.0:80`; the master password crosses the LAN in
   cleartext at login. The Caddy internal-CA edge already fronts Vault (8443) and
   Nextcloud (8444) — the dashboard is the one service left out.
3. **Rate limits are ~3× weaker than written.** Flask-Limiter uses `memory://` storage
   (`src/app.py:414`) with 3 gunicorn workers → per-worker counters ("5/min" on `/login`
   is really up to 15). Redis is already in the stack; point the limiter at it.
4. **`shell=True` drift.** AGENTS.md declares it a non-negotiable invariant; `app.py`
   has ~20 occurrences (mostly constant strings, some `shlex.quote`d interpolations).
   Low exploitability, but the invariant and the code have diverged and nothing enforces it.
5. **Firewall oddities.** `provision.sh:103` opens ufw 18789 for OpenClaw, but the
   gateway binds loopback (`config/openclaw.json:98`) — a dead rule. ufw is only touched
   *if already active*; no default firewall stance.
6. **`newt` mounts `/var/run/docker.sock`** (ro). The one container that talks to the
   internet edge holds a host-root-equivalent handle. Audit whether newt needs it.
7. Small: `login()` uses `==` instead of `hmac.compare_digest` (the 2s sleep mostly
   covers it); `misc` at repo root holds live prod secrets in a public repo's working
   tree — gitignored, but one `git add -A` from a leak. We ship a password manager;
   dogfood it.

### C. Engineering foundation

1. **No CI at all** — no `.github/` directory. The repo is public, so Actions are free.
   A minimal pipeline (shellcheck, ruff + compileall, pytest, `docker compose config`,
   app-import smoke test) would have caught several of the last two months'
   live-discovered bugs (the `.env` newline bug #19, `SCRIPT_DIR`-after-reexec #106,
   missing `configSchema` #108).
2. **Two test files against ~11.5K lines of core code** (`app.py` 3,849,
   `integrations.py` 1,816, `dashboard.html` 3,531, `utilities.sh` 2,265). Rule to
   adopt: *anything that ever broke on hardware gets a regression test.*
3. **Docs mislead the AI-agent workflow.** AGENTS.md says development happens on
   `openclaw-integration` (it's main-based PR branches now) and describes app.py as
   "~1750 lines" (2.2× off). ROADMAP lists recovery phrase and Vault as "in progress" —
   both shipped. Every stale line in AGENTS.md is a bad prompt injected into every
   agent session.
4. Hygiene: ~17 stale local branches; TODO deletion uncommitted.

### D. Product gaps / open threads

1. **Storage expansion has no story.** Nextcloud data is pinned to the root NVMe;
   the prod RPi5 already carries 83G. Drive Management can format/mount backup drives
   but can't say "put my files on the big disk."
2. **G7 from the repoint plan is open:** a domain repoint on a GPU box won't rewrite
   public URLs stored in OpenClaw/MCP configs
   (see `provision-idempotent-tunnel-repoint.md`).
3. **Stock WhatsApp route-restoration is unproven** (2026-06-16 E2E) — verify or delete
   the fallback path.

## Plan

Ordered by user impact. Phase 1 deliberately precedes Phase 2 because Phase 2 rewrites
`backup.sh` — the scariest file to touch without tests.

- **Phase 0 — Hygiene (½ day).** Refresh AGENTS.md + ROADMAP to reality; commit the TODO
  deletion; prune stale branches; move `misc` secrets into Vaultwarden.
- **Phase 1 — Safety net (1–2 days).** GitHub Actions: shellcheck, ruff + compileall,
  pytest, `docker compose config`, app-import smoke. Grep gate for `shell=True`
  (allowlist current sites). Backfill regression tests for previously-bitten logic.
- **Phase 2 — Trust features (the product phase).**
  - **2a. Proactive notifications through the agent.** A systemd timer that checks:
    last backup outcome, disk >85%, SMART health, service crash-loops, update available —
    and pushes plain-language messages via the existing OpenClaw channel (email fallback,
    dashboard banner always). Converts existing infrastructure into "your house texts
    you before something breaks."
  - **2b. Encrypted + verified backups.** Encrypt archives with a key derived from the
    master password (salt in header, self-contained per archive); verify each archive
    after writing; auto-snapshot before every update.
  - **2c. Off-site backup.** Dashboard-configured remote target; copy encrypted archives
    after each backup. Encrypted-at-rest (2b) makes any dumb remote acceptable.
    HomeBrain-to-HomeBrain replication is a later iteration.
- **Phase 3 — Hardening sweep (~2 days).** unattended-upgrades; limiter → Redis;
  dashboard behind the Caddy LAN-HTTPS edge; drop dead 18789 rule + default firewall
  stance; `shell=True` sweep; `compare_digest`; newt docker.sock audit.
- **Phase 4 — Loose ends.** G7 URL-rewrite helper; verify-or-delete stock WhatsApp
  fallback; "move Nextcloud data to another drive" flow.

**Deliberately not on the list** (minimalism guardrails): no frontend framework or build
step, no Prometheus/Grafana (the notifier covers the user need), no multi-box
orchestration, no plugin system, no app.py rewrite — blueprints get extracted only when
a file is being touched anyway.
