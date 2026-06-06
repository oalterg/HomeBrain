# Make `provision.sh` idempotently repoint a box to a new Pangolin tunnel

**Status:** design / findings — implementation deferred ("later")
**Date:** 2026-06-06
**Context:** Operator wants to repoint a live, data-loaded box (e.g. prod RPi5 HomeCloud) to a
new Pangolin tunnel + domain, keeping Nextcloud/Vault/HA data intact, by running `provision.sh`
**idempotently** — a single command, no manual `.env` editing or separate redeploy.

## Product goal

`sudo provision.sh` (with new tunnel creds) on an already-set-up box should, by itself:
1. Update the canonical factory record (`factory_config.txt`).
2. Propagate the new tunnel creds into the live `.env`.
3. Bring the new tunnel live and rewrite NC/HA/Vault trusted domains for the new domain.
4. Preserve all data. Print the new public URLs + a Pangolin resource reminder.

## Current behavior (verified on prod RPi5, beta/`e8d68da`, 2026-06-06)

- Remote mode requires **≥5 positional args**: `NEWT_ID NEWT_SECRET PANGOLIN_DOMAIN PANGOLIN_ENDPOINT FACTORY_PASS [REGISTRAR_URL REGISTRAR_SECRET]`.
- Writes/overwrites `factory_config.txt` from a **fixed key set**.
- Runs system setup (deps, venv, docker image pull, systemd units, GPU hardening if `HAS_GPU`).
- Creates a **temp** `.env` only if none exists (for compose var substitution during pull), then deletes it. **Never modifies an existing real `.env`.**
- **Never** invokes `deploy.sh` / `redeploy_tunnels.sh`; never touches running tunnel containers.
- **Never** touches `.setup_complete` or the data bind-mounts → data-safe.

## Why `provision.sh` alone does NOT repoint today (root cause)

The live `newt` container reads tunnel creds from **`/opt/homebrain/.env`, not `factory_config.txt`**.
NC/HA/Vault trusted domains, `overwrite.cli.url`, Vaultwarden `DOMAIN` are driven from `.env` and
applied by `configure_nc_ha_proxy_settings` during (re)deploy. `provision.sh` updates only
`factory_config` and runs no deploy, so the tunnel + domains stay on the OLD values. The repoint
currently needs a separate `.env` update + `redeploy_tunnels.sh` (today done by the dashboard
`/api/tunnel` endpoint, `app.py:1484`).

## Gaps to implement

**G1 — Propagate factory creds into the live `.env` (already-set-up box).**
When `.env` exists, `.setup_complete` present, and remote args supplied, update the tunnel-identity
keys via `update_env_var`: `NEWT_ID`, `NEWT_SECRET`, `PANGOLIN_ENDPOINT`, `PANGOLIN_DOMAIN`,
`MANAGER_DOMAIN`, `NEXTCLOUD_TRUSTED_DOMAINS=nc.<dom>`, `HA_TRUSTED_DOMAINS=ha.<dom>`,
`VAULT_TRUSTED_DOMAINS=vault.<dom>`, `VAULT_DOMAIN=https://vault.<dom>`. Must **not** touch
`MASTER_PASSWORD`/`MANAGER_PASSWORD`/`MYSQL_*`/`HA_ADMIN_PASSWORD`/`DEPLOYMENT_MODE` or any secret.
Guard each key on its CLI arg being present, so a bare/local re-run is a true no-op.
*This exact mapping already exists in `app.py` `/start_setup` (~723-739) and `/api/tunnel` (~1510-1521).
Factor it into one place — a `common.sh` helper `apply_tunnel_env <id> <secret> <endpoint> <domain>` —
and call it from provision.sh, the wizard, and `/api/tunnel` to stop the three copies drifting.*

**G2 — Drive a redeploy after updating `.env` (only when already set up).**
If `.setup_complete` present, run **`redeploy_tunnels.sh`** at the end (NOT `deploy.sh`): it stops the
old newt, pulls+ups the new tunnel profile, reapplies trusted domains via
`configure_nc_ha_proxy_settings`, restarts NC/HA/Vault. Prefer it because it has **zero wipe logic**.
On a fresh box (no `.setup_complete`) keep current behavior — the setup wizard drives deploy; do not
auto-deploy. Consider a `--apply/--no-apply` flag; recommend auto-redeploy when remote args change an
already-set-up box, but make it skippable.

**G3 — `FACTORY_PASSWORD` forces a change on tunnel-only re-provision.**
Remote mode requires arg5. An operator who only wants to swap the tunnel must restate the existing
factory password; if unknown (this prod box has **no `FACTORY_PASSWORD` at all** — older format),
provision.sh **generates a new random one**. Fix: make FACTORY_PASS optional in remote mode —
preserve the existing `factory_config` value when omitted, generate only if truly absent, and surface
it. Decouple "remote mode" from `argc==5` (detect via presence of newt id/secret/domain).

**G4 — Named flags + defaulting from existing config.**
Add `--newt-id/--newt-secret/--domain/--endpoint/--factory-pass/--registrar-url/--registrar-secret`;
any omitted flag defaults from existing `factory_config` then `.env`. Target UX:
`sudo provision.sh --newt-id X --newt-secret Y --domain miami.homebrain.house` (endpoint + factory
pass inherited). Keep positional form for back-compat.

**G5 — factory_config rewrite drops legacy keys.**
Current rewrite emits only the fixed key set, dropping `NC_DOMAIN`/`HA_DOMAIN` that older boxes (incl.
this prod RPi5) carry. `/api/tunnel/revert` (`app.py:1582-1583`) still reads factory
`NC_DOMAIN`/`HA_DOMAIN`. Either derive those from `PANGOLIN_DOMAIN` in revert and stop persisting
them, or have provision.sh also write `NC_DOMAIN=nc.<dom>`/`HA_DOMAIN=ha.<dom>`. Today re-provision
silently empties them for the revert path.

**G6 — Pre-flight validation before tearing down the working tunnel.**
`redeploy_tunnels.sh` stops old newt then starts new; wrong creds = remote access lost (LAN/dashboard
still fine). Add an optional pre-flight that validates the new newt creds against `PANGOLIN_ENDPOINT`
before switching (or bring new up before removing old). At minimum log a clear "remote access flips —
verify newt logs" warning.

**G7 — Domain-change side effects to audit (mostly handled by redeploy once G2 lands).**
- NC `config.php` (`trusted_domains`, `overwrite.cli.url`, `overwriteprotocol`) — ✓ via `configure_nc_ha_proxy_settings`.
- HA `configuration.yaml` trusted proxies / external URLs — ✓ via `configure_ha_proxy_settings`.
- Vaultwarden `DOMAIN` env — ✓ recomputed per deploy + vault restart.
- **OpenClaw/MCP configs embedding public base URLs** (`nc./ha./vault.<dom>`, self-MCP `public_url`,
  agent channels) — **AUDIT**. N/A on this no-GPU RPi5, but a GPU/x86 box (berlin) may have stored old
  URLs. Consider a helper that rewrites stored public URLs in `~/.openclaw` + MCP env on domain change.

**G8 — External dependency provision.sh can't do: Pangolin org-side resource map.**
The new tunnel needs resources for the new domain: root `<dom>`→manager:80, `nc.<dom>`→8080,
`ha.<dom>`→8123, `vault.<dom>`→8082. Add a post-redeploy reachability probe of the 4 hostnames with a
clear "configure these resources in Pangolin" message on failure.

**G9 — Registrar re-activation on domain change.**
If `REGISTRAR_URL` set, a domain change may require re-activation (email flow). Not set on this box.
Document; consider re-triggering activation when domain changes and registrar present.

**G10 — Idempotency/safety assertions.**
When triggering redeploy, always prefer `redeploy_tunnels.sh` (no wipe) over `deploy.sh`. Add an early
assertion: if `.setup_complete` present, never reach deploy.sh's fresh-install wipe branch. Document
the data-safety contract at the top of provision.sh.

## Verified facts (prod RPi5 — `homebrain.local` / 192.168.1.231, aarch64, 2026-06-06)

- `.setup_complete` present (Dec 21), `install_creds.json` absent → data-safe re-deploy path.
- Current tunnel: `NEWT_ID=r9anwjl58oet1v9`, `PANGOLIN_DOMAIN=berlin.homebrain.house`, `PANGOLIN_ENDPOINT=https://pangolin.homebrain.house`.
- `factory_config.txt` lacks `FACTORY_PASSWORD` + registrar (older format; carries legacy `NC_DOMAIN`/`HA_DOMAIN`).
- `nextcloud-data` 83G (real users incl. `OliAidana`,`admin`), `vault-data` present; all 7 containers up; **no GPU**.
- Deployed code: channel `beta`, ref `e8d68da`; `/opt/homebrain` is **not** a git repo (tarball deploy) — box code can differ from a dev checkout; verify scripts on the box before relying on them.
- `redeploy_tunnels.sh`: no destructive ops. `deploy.sh` wipe gate guarded by `.setup_complete` (`deploy.sh:31`).

## Target one-command UX (after G1–G4)

```
sudo /opt/homebrain/scripts/provision.sh \
  --newt-id 6y83ddoqx41qrwl \
  --newt-secret <token> \
  --domain miami.homebrain.house
# → updates factory_config + .env, redeploys tunnel, preserves data,
#   prints new public URLs + Pangolin resource reminder
```
