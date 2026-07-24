# Product review & improvement plan — July 2026

**Status:** Phases 1–3 complete (CI shipped in #125; Phase 2 in #115 #117 #118 +
replica target; Phase 3 hardening in #120). Caddy LAN-HTTPS edge stays deferred —
both production boxes are remote-mode (Pangolin provides end-to-end TLS) and the
edge would entangle the pre-stack bootstrap flow where the dashboard must run
without containers. Revisit if LAN-only deployments become a real user base.
Post-review closures (v2026.07.20, #126–#132): Telegram-only channels (WhatsApp
deleted, fork retired — resolves Phase 4's "verify-or-delete WhatsApp fallback"),
reboot-required health check, OpenClaw 2026.7.1-2, gunicorn 26 / gevent 26.5 /
redis-py 8 (the held bumps, WS E2E'd), Nextcloud 33. Remaining Phase 4: G7
URL-rewrite helper; "move NC data to another drive" flow. Phase 0 debt: misc
secrets → Vaultwarden.
**Round 2 (2026-07-24):** re-verified every finding against the tree at
`05f9839`. Phases 1–3 hold up. Six new issues found, four of them data-loss
paths that the original review missed — see
[Round 2](#round-2--verification-and-new-findings-2026-07-24) and
[Phase 5](#phase-5--data-recovery-integrity-and-the-agent-trust-boundary).
**Date:** 2026-07-15 (Round 2: 2026-07-24)
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

---

## Round 2 — verification and new findings (2026-07-24)

Verified against the working tree at `05f9839`.

### Confirmed closed

CI exists (`.github/workflows/ci.yml`); the limiter is on Redis (`app.py:441`);
`shell=True` is down to one documented site with a CI gate that fails if the count
drifts (`.github/check_shell_true.py`); `compare_digest` is used in all four auth
paths; `unattended-upgrades` installs from `common.sh:501` and `update.sh:306`; the
dead ufw 18789 rule is gone *and* actively deleted on update (`update.sh:316`); `newt`
no longer mounts docker.sock; backups are GPG-AES256, verified by full read-back
(`backup.sh:439`), and mirrored off-site. The notifier ships with ten checks and
level-transition logic. This is real progress and the phase structure worked.

### Stale in the docs

`AGENTS.md:59` still says app.py is "~1750 lines" — it is **4,045**, and the repo
layout block omits `recovery.py`, `integrations.py`, and `src/static/`. `ROADMAP.md`
still lists the recovery phrase, Vault, and OpenClaw integrations as *"in progress,
branch X"*; all three shipped. Phase 0's "refresh AGENTS.md + ROADMAP" was never done,
and the original finding stands: every stale line is a bad prompt injected into every
agent session. Phase 1 also promised an **app-import smoke test** — `ci.yml` runs
`compileall` but never imports `app`, so module-level breakage still ships.

### New findings

**1. Restore is broken on internal-storage boxes.** `restore.sh:18` hard-requires
`mountpoint -q "$BACKUP_MOUNTDIR"`. `BACKUP_INTERNAL` is handled in `backup.sh:93`,
`update.sh:185`, `healthcheck.py:156`, and `app.py:645` — but not in restore.sh. PR
#123 added no-drive mode and never taught restore about it. The dashboard lists the
archives correctly (`backup_storage_dir()` resolves), passes a valid absolute path, and
every restore dies at line 20. A supported configuration can back up and can never
restore. The two mount guards drifted because each script owns its own copy.

**2. A recovery-phrase reset orphans every existing backup.** Archives are encrypted
with `MASTER_PASSWORD` as it stood at backup time (`backup.sh:167`);
`rotate_master_password.sh` step 4 replaces it. The recovery flow's entire premise is
that the user has forgotten the master password — so afterwards every archive, local
and off-site, needs a passphrase they by definition do not have. `restore.sh` supports
`RESTORE_PASSPHRASE_FILE` for exactly this, so the *mechanism* exists and only the
*knowledge* is gone. Nothing warns before rotating and nothing takes a fresh backup
after. The one flow designed to save a locked-out user destroys their backup history.

**3. `rclone sync` makes the off-site a mirror, not a backup.** `common.sh:817` uses
`sync`, which propagates deletions. Combined with the emergency prune loop
(`backup.sh:136`, which deletes old archives to free space) and verification-failure
deletion (`backup.sh:444`), a local disk problem replicates itself off-site on the next
run. Ransomware or an accidental wipe takes both copies. Related: the prune deletes
existing *good* archives to make room before the replacement is known-good, so a run
whose verification fails can end with strictly fewer backups than it started with.

**4. There is no off-site *restore*.** `/api/backup/offsite` writes config and
`offsite_sync` pushes; nothing pulls. `restore.sh` only ever reads `$BACKUP_MOUNTDIR`.
The exact scenario off-site exists for — box or drive destroyed — requires a user who by
design never SSHes to hand-run rclone and gpg on fresh hardware. The feature shipped
without its recovery path, which means it is currently a backup we have never proven we
can restore.

**5. The agent is already root, and the config pretends otherwise.**
`config/openclaw.json:85` sets `approvals.exec = {enabled: true, mode: "session"}` —
approve once, then unattended for the rest of the session — for an agent with
`tools.profile: "full"` that ingests email and browses the web, both prompt-injection
vectors. But the more important fact is upstream of that flag: `common.sh:136` puts
`homebrain` in the `docker` group, and any member of `docker` can `docker run -v /:/host`
and read or write anything on the box. **The agent is root-equivalent today.** File
permissions on `.env` cannot contain it, and an OpenClaw-level path deny-list would be
theater even if it were expressible — which it is not: `utilities.sh:1465` records that
stock OpenClaw ≥2026.6 strictly validates config and refuses to load the gateway on
unknown keys (the `mcp: Invalid input` incident). Separately, `homebrain` has a locked
password and no NOPASSWD rule, so the agent cannot actually `sudo` at all right now —
the approval gate is guarding a door that is already open elsewhere while the intended
door is nailed shut.

*Verified live on .58 (2026-07-24):* `groups` returns `… sudo … docker render`;
`sudo -n true` fails with "interactive authentication is required"; the live
`openclaw.json` carries `{"exec": {"enabled": true, "mode": "session"}}`. All three
match the code reading. One thing did **not**: `/opt/homebrain/.env` is
`-rw------- homebrain homebrain`, not root-owned, inside a root-owned directory and with
no `chown` anywhere in `scripts/`. So the agent does not even need the docker escape —
it owns the file holding `MASTER_PASSWORD` and can `cat` it. The cause is unidentified
(no code path in the tree produces this), so it is either drift on this box or an
unexamined write path; either way the fix is an assertion rather than a hunt. Note also
that `HOMEBRAIN_INTEGRATIONS_KEY` — the Fernet key wrapping every stored account token —
sits in plaintext in the homebrain-owned `openclaw.json` because the MCP servers need it
injected. That is inherent to the design, not a defect, but it is another reason the
filesystem is not the boundary.

**6. `shenxn/protonmail-bridge:latest`** (`docker-compose.yml:162`) is the one unpinned
image in a stack whose pinning discipline is otherwise exemplary, and it is the one
handling mail credentials. Version pinning is a documented invariant; this is drift.

### Also noted, not scheduled

No log rotation for `/var/log/homebrain` (backup/restore/manager logs and
`mcp-*-audit.log` grow forever), and five of ten compose services set no `logging:`
limits (`redis`, `homeassistant`, `newt`, both `cloudflared`). No dead-man's switch —
`healthcheck.py` runs on the box, so a dead box is silent, which is indistinguishable
from healthy. The notifier is Telegram-or-nothing (`healthcheck.py:388`) despite Phase
2a promising an email fallback, and SMTP credentials are already in the stack. `app.py`
(4,045 lines) and `integrations.py` (1,593) still have zero tests. No `dependabot.yml`.
No container hardening (`cap_drop`, `no-new-privileges`, memory limits) anywhere.
Nextcloud publishes `0.0.0.0:8080` plaintext (`docker-compose.yml:44`) while Vaultwarden
is correctly loopback-only and Caddy already terminates TLS for NC on 8444. `misc` at
repo root still holds live production credentials including a Home Assistant long-lived
token valid into 2036 — those should be treated as disclosed and rotated, not merely
moved. Smaller: `backup.sh:69` removes the lock file while still holding the flock, so
two runs can overlap during cleanup; `backup.sh:117` calls `docker compose` without
`--env-file`, unlike every other call site; no login lockout and no failed-login
notification through the notifier that already exists.

---

## Phase 5 — data recovery integrity and the agent trust boundary

Scope: Round 2 findings 1–4, 5, and 6. Ordered so that each step lands on a tree where
the previous one has already removed a way to lose data. 3 before 4 is the one hard
dependency — there is no point building a restore path from a mirror that deletes
itself.

### 5a. Single source of truth for the backup directory (finding 1)

Extract the mount guard both scripts need into `common.sh` as `ensure_backup_dir()`:
honour `BACKUP_INTERNAL=true` by asserting the directory exists, otherwise
`mountpoint -q` and fall back to `mount`. Call it from `backup.sh:93` and `restore.sh:18`.
The fix is not "add the branch to restore.sh" — it is to stop having two copies that can
drift, which is what produced the bug.

*Test:* `scripts/tests/test_restore_internal.sh` — run `restore.sh` against a temp dir
with `BACKUP_INTERNAL=true` and assert it gets past the guard to the archive-selection
step. Wire into `ci.yml`.

### 5b. Off-site copies survive local deletion (finding 3)

Add `--backup-dir "offsite:${OFFSITE_PATH}/replaced/$(date +%F)"` to the `rclone sync`
at `common.sh:817`. One flag: deletions and overwrites become moves, so a local wipe can
no longer erase the remote copy. Prune `replaced/` on the same retention pass, keeping
90 days. Separately, move the emergency prune (`backup.sh:136`) to run *after* the new
archive verifies rather than before it is written, so a failed run can never leave the
user with fewer backups than they started with.

*Test:* extend `test_backup_encryption.sh` with a local-filesystem rclone remote —
sync, delete the local archive, sync again, assert the remote copy still resolves under
`replaced/`.

### 5c. Rotation stops orphaning backups (finding 2)

Three small changes, no re-encryption — re-encrypting old archives would need the old
password, which in the recovery case is precisely what is missing:

- `rotate_master_password.sh` step 4: after `MASTER_PASSWORD` lands, record the rotation
  timestamp in `/var/lib/homebrain/backup_epoch.json`.
- Same script, at the end: trigger a full backup so a current-password archive exists
  before the user closes the tab.
- Dashboard: archives older than the recorded epoch get a "needs your previous password"
  badge in the restore list. The passphrase field in the restore modal already exists and
  already works — this only has to tell the user *when* to use it.
- Recovery-reset UI: state plainly, before the user commits, that existing backups will
  need the old password and that a fresh one will be taken automatically.

### 5d. Restore from off-site (finding 4)

`restore.sh --from-offsite <archive-name>`: reuse `offsite_env`, `rclone copy` the single
named archive into `$BACKUP_MOUNTDIR`, then fall through to the existing restore path
unchanged. No new restore logic and no second code path to keep in sync. Add
`utilities.sh offsite_list` wrapping `rclone lsjson` so the dashboard can show remote
archives in the existing backup list with a badge, reusing `/api/restore` with a
`source` flag.

**Scoped out, deliberately:** bare-metal recovery onto a *new* box (off-site creds
entered in the setup wizard before a stack exists) is the larger half of this problem and
needs wizard UI. Phase 5d covers "drive died, box alive," which is the common case and
is reachable from the dashboard. The bare-metal path gets a documented runbook in
`docs/` now and wizard support in a later phase — but note that until that ships, the
disaster-recovery story still has a manual step.

*Test:* the round-trip that has never existed — back up, encrypt, push to a local rclone
remote, wipe the local copy, restore from off-site, diff the tree. This is the single
most valuable test in the repo and should gate CI.

### 5e. Make the agent's trust boundary explicit (finding 5)

The decision is to let the agent run privileged commands unattended — that is what "no
SSH required" means, and `docker` group membership already grants it root equivalence, so
the current config buys no safety while blocking the intended workflow.

- `config/openclaw.json` and `utilities.sh:1470`: set `approvals.exec.enabled = false`
  and drop `.mode`. Leave `approvals.plugin` at `session` — plugin installs are rare,
  user-initiated, and not on the automation path.
- Add `/etc/sudoers.d/homebrain` granting NOPASSWD, written via a temp file and validated
  with `visudo -c` before install. This does not widen the blast radius — it makes the
  existing docker-group equivalence explicit and usable instead of leaving the agent to
  discover the container escape hatch.
- Assert `root:root 0600` on `.env` — not just the mode. On .58 it is currently
  `homebrain:homebrain 0600`, so the agent can read `MASTER_PASSWORD` with `cat` and
  never has to reach for the docker escape. Enforce ownership and mode at manager boot
  and at every write path (`app.py:762` sets the mode only; the bash creation sites
  `common.sh:241` and `provision.sh:239` use the default umask). This does not contain a
  root-equivalent agent, but it restores the intended asymmetry and still defends against
  a container escape landing as `www-data`.
- Document the boundary in AGENTS.md security invariants, in one sentence: **the agent is
  root on this box; the security boundary is the Telegram `allowFrom` pairing and the
  loopback-bound gateway, not the tool sandbox.** Anyone reasoning about agent risk needs
  to start from that, and today the config implies the opposite.

**No path deny-list.** Unknown keys brick the gateway (`utilities.sh:1465`), and a
deny-list a root-equivalent process can walk around is theater that would make the next
reviewer think the box is safer than it is.

### 5f. Pin the Proton bridge (finding 6)

Read the digest currently running on both boxes
(`docker inspect --format '{{index .RepoDigests 0}}'`), record it under
`versions.json` as `proton_bridge`, and reference it from compose as
`${PROTON_BRIDGE_TAG:-<digest>}`, following the `VAULTWARDEN_TAG` precedent at
`docker-compose.yml:120`. Pin to what is already running and verified, not to whatever
`latest` resolves to on the day of the change.

*Blocked on a digest:* .58 has no Proton account configured, so the profile is inactive
and the image is not present (`no such object`). Take the digest from a box where the
bridge actually runs — berlin or miami — before writing the pin. Pulling `latest` here
just to read its digest would defeat the point of pinning to a verified build.
