# HomeBrain Testing Guide

A living checklist for verifying HomeBrain changes on real hardware. Run this before merging any feature that touches provisioning, services, the dashboard, or the AI stack.

---

## Pre-flight

Before running any tests, confirm the environment is ready:

- [ ] SSH into the target device (`ssh homebrain@homebrain.local` or via Pangolin URL)
- [ ] `sudo systemctl is-active homebrain-manager` → `active`
- [ ] `docker compose ps` — all expected containers are `Up` and healthy
- [ ] `cat /opt/homebrain/.env` — `DEPLOYMENT_MODE`, `MASTER_PASSWORD`, and tunnel vars are set correctly
- [ ] Confirm target variant: `uname -m` (aarch64 = HomeCloud/RPi5, x86_64 = HomeBrain/AMD)
- [ ] Confirm GPU presence (HomeBrain only): `ls /dev/dri/renderD*` → device node exists

---

## Deployment targets

Run applicable sections for the configuration under test.

### A — Remote Access (Pangolin tunnel)

External HTTPS access via a Pangolin-managed subdomain.

- [ ] `DEPLOYMENT_MODE=remote` in `.env`
- [ ] Newt container is running and tunnel is established (`docker logs newt` shows connected)
- [ ] Dashboard loads over the public HTTPS URL without certificate errors
- [ ] Nextcloud accessible externally: login, upload a file, confirm it persists
- [ ] Home Assistant accessible externally: dashboard loads, an entity state is visible

### B — Local Network Only (mDNS)

No external tunnel; access via `homebrain.local`.

- [ ] `DEPLOYMENT_MODE=local` in `.env`
- [ ] No Pangolin/Newt container running
- [ ] Dashboard loads at `http://homebrain.local:8000` (or configured port)
- [ ] Nextcloud accessible at its configured local URL
- [ ] Home Assistant accessible at its configured local URL

---

## Configurations

### C — With GPU (AI stack active, HomeBrain only)

- [ ] `lspci | grep -i vga` shows the AMD GPU
- [ ] `llama-server` systemd service is active and bound to its port
- [ ] OpenClaw container is running
- [ ] Dashboard GPU stats card shows utilisation, temperature, and VRAM values (not zeros/errors)
- [ ] AI inference test: send a message via OpenClaw chat → response received
- [ ] Log viewer in dashboard shows live llama-server and OpenClaw log entries

### D — Without GPU (AI stack disabled gracefully)

- [ ] No GPU present or GPU detection returns false
- [ ] `llama-server` service is not started (or masked) — no error in journalctl
- [ ] OpenClaw container is not running
- [ ] Dashboard loads without GPU stats card or shows a clear "AI unavailable" state
- [ ] No unhandled errors in `/var/log/homebrain/` related to missing GPU

---

## Functional areas

### Provisioning / Setup wizard

- [ ] Fresh-install: run `provision.sh` end-to-end without errors
- [ ] Setup wizard loads at first boot (`/setup` route)
- [ ] Deployment mode selector (Local / Remote Access) sets `.env` correctly
- [ ] Credentials page shows generated username + password exactly once, then deletes staging file
- [ ] Re-running `deploy.sh` is idempotent (no errors, services remain healthy)

### Core services health

- [ ] All containers in expected `Up` state: `db`, `redis`, `nextcloud`, `homeassistant`
- [ ] `homebrain-manager` survives a restart: `sudo systemctl restart homebrain-manager`
- [ ] Background task spawned from dashboard (e.g. backup) reaches completion and updates status

### Dashboard

- [ ] Login page loads; rate limiting blocks >5 failed attempts per minute
- [ ] Session cookie is `HttpOnly` + `SameSite=Lax` (check browser devtools)
- [ ] Status cards reflect actual container states (start/stop a container, refresh)
- [ ] Log viewer loads entries for at least one service without errors

### AI inference (HomeBrain / GPU only)

- [ ] OpenClaw chat UI accessible from pre-authenticated dashboard link (no token prompt)
- [ ] WhatsApp message → OpenClaw → llama-server → response delivered end-to-end
- [ ] Gateway token is stable across a `homebrain-manager` restart

### HomeBrain Vault (Vaultwarden)

- [ ] `docker compose ps vaultwarden` shows the container `Up` and healthy
- [ ] Dashboard tile reflects status (`HEALTHY` / `RUNNING`), shows public URL, and exposes an "Open Vault" link
- [ ] First-run modal: bootstrap with an email → response is `invited` (or `already_bootstrapped`); `VAULT_SIGNUPS_ALLOWED` flips to `false` in `.env`
- [ ] Vault data dir exists at `/home/homebrain/vault-data` with `rsa_key.pem` (or `rsa_key.pkcs8.der`) after first user signup
- [ ] DB user `vaultwarden_user` has grants ONLY on the `vaultwarden` database (`SHOW GRANTS FOR 'vaultwarden_user'@'%'`)
- [ ] `VAULT_ADMIN_TOKEN` in `.env` is an Argon2id PHC string (`$argon2id$…`), NOT plaintext
- [ ] Bitwarden browser extension: log in with `Self-hosted` server URL → add a credential → it persists across reload
- [ ] Bitwarden mobile app (remote mode): log in over `vault.<tunnel>` → credential syncs from the browser session in <2s
- [ ] Add a 2 MB PDF as a credential attachment → file lands under `vault-data/attachments/` and decrypts on a second client
- [ ] Send: create a one-time text Send → expires server-side after first view
- [ ] Trigger a backup → archive contains `vault_db/vaultwarden.sql` and `vault_data/rsa_key.*` (live sessions survive)
- [ ] Wipe `/home/homebrain/vault-data` and restore from archive → all clients still authenticate, attachments decrypt
- [ ] Bump `vaultwarden.tag` in `versions.json` → click Update → container recreated, no data loss, sessions still valid
- [ ] Switch deployment mode `local` ↔ `remote` → `VAULT_DOMAIN` updates, vaultwarden container restarts, clients re-resolve
- [ ] Stop vaultwarden manually → dashboard tile flips to `STOPPED`; `docker compose up -d` restores it; `restart: unless-stopped` re-attaches after host reboot

### Backup and restore

- [ ] Trigger backup from dashboard → task completes, backup archive written to external drive
- [ ] Backup excludes NVMe/system drives from candidate list
- [ ] OpenClaw config and agent workspace are present in backup archive
- [ ] Restore from archive → Nextcloud and Home Assistant data intact after restore

### Always-on behaviour (HomeBrain only)

- [ ] `cat /sys/bus/pci/devices/.../power/control` → `on` (GPU PM disabled)
- [ ] Sleep inhibitor service is active: `sudo systemctl is-active inhibit-sleep`
- [ ] Crash-loop protection: kill `homebrain-manager` process → systemd restarts it within configured limit

### Nextcloud

- [ ] File upload and download work end-to-end
- [ ] Nextcloud cron job runs without errors (`utilities.sh` cron path)

### Home Assistant

- [ ] Dashboard loads and shows at least one entity
- [ ] Admin account created via `utilities.sh` is usable

---

## Sign-off checklist

Complete before merging to `main`:

- [ ] All applicable sections above pass on the target hardware
- [ ] No new errors in `/var/log/homebrain/` or `journalctl -u homebrain-manager`
- [ ] `docker compose ps` shows all expected services healthy after a full stack restart
- [ ] Tested both fresh-install and re-run (idempotency) where relevant
- [ ] PR description notes which sections were tested and on which variant/configuration
