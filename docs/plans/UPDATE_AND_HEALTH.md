# Update, OS upgrade, and health

**Status:** investigation (2026-09-13); the holes below are closed in tree.
**Essence:** Three separate paths — HomeBrain app update, OS patching, and
a notify-only health checker. They are not one system.

Related: `scripts/update.sh`, `scripts/healthcheck.py`,
`src/app.py` (`/api/manager/update`, `/api/upgrade`, `/api/system/reboot`,
`/api/health`), `config/homebrain-health.{service,timer}`,
[`PRODUCT_REVIEW_2026-07.md`](PRODUCT_REVIEW_2026-07.md) (Phases 2a / 3
shipped the notifier and unattended-upgrades).

---

## 1. The three paths

| Path | Trigger | What it changes | Rollback |
|---|---|---|---|
| **App update** | Dashboard *App update* → `POST /api/manager/update` → `update.sh` | `/opt/homebrain` tree, pinned deps, systemd units, compose image pins, Nextcloud schema | None for the release. The pre-update snapshot is data/config only — not the previous code, image pins, or deps. Restore runs on whatever release is installed now and may repeat that release's migration. Updates are one-way. |
| **OS, nightly** | `unattended-upgrades` (enabled by `update.sh` writing `20auto-upgrades`) | Distro security origin. No automatic reboot. | Package manager. Kernel waits for a manual restart. |
| **OS, button** | Dashboard *Run now* → `POST /api/upgrade` | The same `unattended-upgrade` path, on demand. No docker compose. | None taken. Failed apt no longer reports success. |

Health does not apply updates. It reports that a HomeBrain release exists,
or that `/var/run/reboot-required` is set. **Restart box**
(`POST /api/system/reboot`) is the button for the latter. Nothing
auto-installs a HomeBrain release. That is intentional.

Service crash recovery is systemd `Restart=always` and Docker
`restart: unless-stopped`. Health does not restart anything.

---

## 2. App update (`update.sh`)

1. Fetch `update.sh` + `common.sh` from GitHub raw (tag for stable, `main`
   for beta). Re-exec if they differ, so the running updater is always the
   target's. After re-exec, `SCRIPT_DIR` is `/tmp/homebrain_self_update`;
   later steps must resolve helpers from `/opt/homebrain`, not `$SCRIPT_DIR`.
2. **Downgrade guard, twice** — before re-exec (so an older fetched script
   cannot skip the guard) and again against the extracted tree. Blocks
   Nextcloud tag regression and stable-channel rollback.
   `ALLOW_DOWNGRADE=1` is the explicit override. Tests:
   `scripts/tests/test_update_guard.sh`.
3. Rsync the release over `/opt/homebrain`, keeping `.env`, `version.json`,
   venv, compose override, `.platform.json`.
4. Pre-update **system** snapshot via `backup.sh --strategy system
   --skip-offsite` (data/config, not the Nextcloud file tree, and not the
   app tree / image pins / deps). It is a data restore point, not an app
   rollback: restoring it uses the currently installed release and may
   re-run that release's Nextcloud migration. Non-fatal if no backup drive.
   Then pin bumps (llama.cpp / OpenClaw / Vaultwarden),
   systemd unit sync, `docker compose pull/up` with `get_runtime_profiles`
   (tunnel profiles plus a running `proton-bridge`, so `--remove-orphans`
   does not stop it), `reconcile_nextcloud`, write `version.json`, restart
   `homebrain-manager`.

Compose image tags are pinned. App updates are what move Nextcloud / HA /
Vaultwarden versions; a pull without a pin change only refreshes the same
tag's digest.

Stable vs beta: the channel dropdown sits with **App update**. OS upgrade
ignores it.

`/api/manager/update` claims the shared task slot (`log_type=update`) so the
global banner tracks the run and a second long job 409s. The claim is a
file lock across Gunicorn workers — backups, restores, OS upgrade, reboot,
and the other long jobs use the same reservation. The manager restart at
the end of `update.sh` clears the status file on startup.

Check-now uses `stable_update_offer` / `beta_ahead` (same ordering as
healthcheck), not string inequality.

---

## 3. OS upgrades

Nightly is the NAS policy: security origin, no reboot, health nag when a
kernel/libc patch is waiting. `update.sh` writes `APT::Periodic::*` in
`20auto-upgrades`. Allowed origins and `Automatic-Reboot` (off) are distro
defaults, not pinned in-repo — "security only" holds on Debian and may not
on Ubuntu / Raspberry Pi OS.

The dashboard button is the same command, on demand:

- `apt-get update && apt-get install unattended-upgrades && unattended-upgrade`
- `Dpkg::Options` force-confold, `DEBIAN_FRONTEND=noninteractive`
- `&&` throughout, so a failed apt-get cannot still echo success
- No `apt-get upgrade` of every installed package, no `docker compose`
  pull/up (those belong to app update)

`apt-get upgrade` never installs new packages; `unattended-upgrade` can
(kernel ABI bumps). The button used to be inverted. It is not anymore.

Restart is never automatic. Health warns; **Restart box** (and the banner
button when the reboot check is present) confirms, then `systemctl reboot`.

---

## 4. Health (`healthcheck.py`)

`homebrain-health.timer`: 5 min after boot, then every 30 min.

Stdlib only (`/usr/bin/python3`). Checks: backup schedule/freshness, off-site
copy, root / backup / files-drive disks, SMART, systemd units, compose
health, Pangolin tunnel (log state, not "container running"), OpenClaw
gateway, newer stable release, reboot-required. GPU-gated checks read
`.platform.json`, not `/dev/dri`.

Notify on **level transitions**, not every run. Reminders: crit 24 h, warn
7 d. Telegram via `openclaw message send` (no LLM — alerts still go when
llama-server is down). Email only if push did not go. Optional
`HEARTBEAT_URL` POST so a dead box is not silent (owner-deployed Worker;
off unless armed).

Dashboard banner reads `/var/lib/homebrain/health.json` through
`GET /api/health`. A report older than 2 h (or a missing file) is
`overall: unknown` with `stale: true`; the banner **shows** that, it does
not hide it. Tests: `scripts/tests/test_healthcheck.py`,
`scripts/tests/test_health_banner.py`, `scripts/tests/test_update_health.py`.

---

## 5. What not to confuse

- *App update* ≠ *Run now* (OS). Different endpoints, different risk,
  different rollback.
- A pre-update snapshot ≠ rolling the app back. It restores data/config onto
  the currently installed release, which may repeat that release's migration.
- Nightly unattended **is** the OS button. Both security-origin; neither
  reboots.
- Health ≠ healer. It tells the owner. systemd/Docker restart crashed
  processes. `reconcile_nextcloud` self-heals schema only on the app-update
  path.
- Compose `healthcheck` blocks ≠ `healthcheck.py`. The former keep
  `depends_on: service_healthy` honest. The latter is the owner-facing
  report.
