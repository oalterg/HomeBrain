# HomeBrain Testing Guide

A living checklist for verifying HomeBrain changes on real hardware. Run this before merging any feature that touches provisioning, services, the dashboard, or the AI stack.

---

## Pre-flight

The hardware test target is shared. More than one agent session runs against
it, and `nuclear_reset.sh` / `provision.sh` / `restore.sh` wipe the machine.
Take the lock before any write (`docker compose` included). A read is fine
without it.

```bash
LOCK=/home/admin/using_test_target
ssh admin@<target> "cat $LOCK 2>/dev/null"          # who has it, if anyone
ssh admin@<target> "echo 'holder: <session-id> (<what you are doing>)' > $LOCK"
ssh admin@<target> "rm -f $LOCK"                    # when you are done, including failure
```

`/home/admin` survives the reboots a reset causes (`/tmp` does not). A missing
file does not mean idle: `pgrep -f 'scripts/(deploy|provision|nuclear_reset|restore|update)\.sh'`
before claiming. If results contradict what you did, check the lock and `who`
before believing them.

Before running any tests, confirm the environment is ready:

- [ ] You hold `$LOCK` (or you are only reading)
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
- [ ] Reboot the box: `docker logs newt` reaches "established" on its own, with no `systemctl restart docker` (the tunnel used to stay down after every reboot because Docker had snapshotted a pre-DHCP nameserver)

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
- [ ] Setup wizard is reachable as soon as provision finishes (reboot is not a gate)
- [ ] HomeBrain (GPU): wizard defaults to Local network, copy names Telegram as the remote path, Pangolin is optional and not auto-selected even if `factory_config` has Newt creds
- [ ] HomeCloud (no GPU): Local / Remote access; factory Newt creds auto-select Remote; initializing without re-typing the secret succeeds
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
- [ ] Telegram message → OpenClaw → llama-server → response delivered end-to-end
- [ ] Gateway token is stable across a `homebrain-manager` restart
- [ ] `~/.openclaw/workspace/HEARTBEAT.md` has a Current work section (not headings-only)
- [ ] `jq -r '.agents.defaults.memorySearch.provider' ~/.openclaw/openclaw.json` is `none`
- [ ] Hourly heartbeat does not send a status summary to Telegram (`heartbeat.target` unset)

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

### Vault — LAN HTTPS (Caddy + local CA)

- [ ] `caddy` container reaches healthy within 30 s of first boot
- [ ] In local mode `VAULT_DOMAIN` is `https://homebrain.local:8443`
- [ ] `curl -k https://homebrain.local:8443/healthz` returns HTTP 200
- [ ] Browser at `https://homebrain.local:8443/` shows the Bitwarden web vault (cert warning expected until CA is installed)
- [ ] `/api/vault/local-ca` returns a PEM file (mode 600 disposition); installing it on a phone removes the warning
- [ ] After CA install, Bitwarden Android app at `https://homebrain.local:8443` connects without trust errors
- [ ] WebSocket sync works: edit a credential in browser ext → mobile updates within 2 s
- [ ] Mode flip local → remote → `redeploy_tunnels.sh` restarts caddy + vaultwarden; `VAULT_DOMAIN` updates
- [ ] In remote mode, `/api/vault/local-ca` returns 404 (Pangolin's public chain is used)

### Vault — encrypted documents (Nextcloud E2EE)

- [ ] Vault tile shows `E2EE app: DISABLED`, `Folder: MISSING` on a fresh install
- [ ] Click "Set up encrypted folder" → response 200, banner says "Ready"
- [ ] Within 10 s tile shows `E2EE app: ENABLED`, `Folder: CREATED`, "Open in Nextcloud" link appears
- [ ] `Documents (Encrypted)` folder visible in the Nextcloud UI for the admin user
- [ ] Re-running setup is idempotent (no-op + same status)
- [ ] Nextcloud client app prompts to mark the folder as encrypted; once accepted, files added there are E2EE

### Vault — OpenClaw MCP (GPU only)

- [ ] `bw` CLI installed (`npm install -g @bitwarden/cli`); tile shows `CLI installed: YES`
- [ ] Tile shows `Session: LOCKED`; unlock input is visible
- [ ] Unlock with the wrong master password → returns 401, tile stays LOCKED
- [ ] Unlock with the correct master password → response 200, tile flips to `UNLOCKED`
- [ ] Session token persisted at `/home/homebrain/.openclaw/vault.session` mode 600, owner `homebrain`
- [ ] Run `python3 /opt/homebrain/scripts/mcp-vault.py` and pipe a JSON-RPC `tools/list` → returns vault.search/vault.reveal/vault.status
- [ ] `tools/call` `vault.status` → `{unlocked: true, url: …}`
- [ ] `tools/call` `vault.search` with a query → returns metadata only (no `password` field)
- [ ] `tools/call` `vault.reveal` with a valid item ID → returns password + writes to `/var/log/homebrain/mcp-vault-audit.log`
- [ ] Click "Lock session" → session file removed, tile flips to LOCKED
- [ ] After lock, MCP server returns `{unlocked: false, hint: …}` for any tool call

### Backup and restore

- [ ] Trigger backup from dashboard → task completes, backup archive written to external drive
- [ ] Backup excludes NVMe/system drives from candidate list
- [ ] OpenClaw config and agent workspace are present in backup archive
- [ ] Restore from archive → Nextcloud and Home Assistant data intact after restore

**Portable instance secrets survive the round trip.** This is the check that would have caught
the truncation bug: for eight months `restore.sh` shortened `HOMEBRAIN_EMAIL_KEY` from 44 to 43
characters on *every* restore, and nothing anywhere reported it — the MCP servers handed the
still-encrypted token to Home Assistant as a bearer token and the user saw a bare 401.

- [ ] Before backing up: `sudo grep '^HOMEBRAIN_EMAIL_KEY=' /opt/homebrain/.env` — note the value
- [ ] After restoring: **same value, still 44 characters, still ending in `=`**
      (a 43-character key means the merge is truncating again)
- [ ] A stored account token still decrypts:
      `sudo python3 -c "import sys,json;sys.path.insert(0,'/opt/homebrain/scripts');from mcp_common import decrypt_secret;print(decrypt_secret(json.load(open('/home/homebrain/.openclaw/ha_accounts.json'))['accounts'][0]['token'], KEY))"`
      → the plaintext token, **never** a string starting `gAAAAA`
- [ ] Truncate the key by hand, restart `homebrain-manager`, and confirm the dashboard repairs it
      back to 44 characters and logs "Repaired truncated HOMEBRAIN_EMAIL_KEY"

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

## OpenClaw integrations (Connections page)

E2E on `homebrain@192.168.178.58`. Run after a fresh provision plus the
default Vault bootstrap.

### General

- [ ] Dashboard → Status tab shows the **Connections** card with five rows.
- [ ] On a fresh box, `homebrain-self` is the only row marked `wired`; the
      rest are `not configured`.
- [ ] Clicking **Apply & restart agent** runs `/api/integrations/reconcile`,
      OpenClaw daemon restarts within ~10 s, no errors in
      `journalctl -u homebrain-manager`.

### Self MCP (`homebrain-self`)

- [ ] `/home/homebrain/.openclaw/homebrain.token` exists, mode 0600.
- [ ] `curl -H "Authorization: Bearer $(cat ~/.openclaw/homebrain.token)" \
      http://127.0.0.1/api/integrations/self/status` returns 200.
- [ ] `connTest('self')` returns 10 tools.

### Home Assistant

- [ ] Generate LLAT in HA → Profile → Security; paste into the dashboard;
      Connect → row flips to `wired`.
- [ ] `connTest('homeassistant')` lists 5 tools.
- [ ] `ha.call_service` with `homeassistant.restart` returns the allowlist
      denial without any HTTP call to HA.

### Nextcloud

- [ ] Click **Connect** → `occ user:add-app-password` runs, token at
      `~/.openclaw/nextcloud.token` mode 0600.
- [ ] `connTest('nextcloud')` lists 8 tools.
- [ ] `nc.notes_create` (with consent) appears on a separate NC client.
- [ ] Revoking the app password in NC's UI flips `nc.health` to
      `unauthorised` within one health-check cycle.

### Vault (existing + new)

- [ ] All `VAULT_PLAN.md §7` steps still pass.
- [ ] `vault.create_login` (with consent) appears in the Bitwarden mobile
      app within 2 s; audit log records the action with `chat_id`.

### Email

- [ ] Add a Gmail account using an app-specific password; row shows
      `wired`.
- [ ] `email.list_unread` returns inbox metadata, no bodies.
- [ ] `email.draft` creates a Gmail draft; `email.send_direct` is denied
      (`disabled`) until the Settings toggle flips it on.
- [ ] Proton account: `docker compose --profile proton-bridge up -d`
      starts Bridge; `imap_host=127.0.0.1`, `imap_port=12143` works.

### Channel linking — Telegram (stock upstream OpenClaw)

HomeBrain runs **stock npm `openclaw`** (no fork, no channel plugins).
Telegram is the only supported channel and is bundled in core.

- [ ] Paste a bot token → row flips to configured; daemon restarts.
- [ ] Send `/pair` from the bot, approve the code in the dashboard
      (`openclaw pairing approve telegram <code> --notify`) → DM works.
- [ ] On an upgraded box that previously had WhatsApp: `openclaw plugins list`
      shows neither `homebrain-whatsapp-login` nor `@openclaw/whatsapp`, and
      `~/.openclaw/openclaw.json` has no `.channels.whatsapp` /
      `.plugins.entries.whatsapp` keys (one-shot migration in
      `patch_openclaw_config` + `remove_whatsapp_plugins`).

### Cross-cutting

- [ ] All `*.token`, `vault.session`, `email_accounts.json` are mode 0600
      owned by `homebrain`.
- [ ] Single-use redemption: replaying a confirmation_token a second
      time returns "invalid or expired".
- [ ] `backup.sh` archive contains `openclaw_integrations/` and
      `mcp_audit/` trees; `restore.sh` repopulates them with mode 0600.

### HA watchers (GPU only)

Plan: [`plans/HA_WATCHERS.md`](plans/HA_WATCHERS.md). Take `$LOCK` before
writes. Unit tests cover fire/cooldown/seed; this is the box check.

- [ ] `systemctl is-active homebrain-ha-watch` → `active` (OpenClaw present)
- [ ] `homebrain.watcher_set` an `input_boolean` on local HA (`to: on`,
      `wake: false`) → flipping it in HA sends one Telegram text ping.
      Flip again within 120s → silence. Cold start / daemon restart →
      silence.
- [ ] Same watcher on a remote HA account → ping still arrives.
- [ ] Watcher with `camera_entity_id`: still arrives as a photo while
      llama-server is stopped. One still, not two.
- [ ] `wake: true`: a follow-up clerk turn may message; it must not
      append to the main Telegram session (`openclaw sessions` shows
      session key `ha-watch`, not the DM). No duplicate still. No
      self-fired siren.
- [ ] Restore from backup containing `ha_watchers.json` → daemon seeds
      and stays quiet.
- [ ] Full sentence: watcher ping + `ha.automation_upsert` porch light
      on that account. Light still works if this GPU is down.

---

## Master-password recovery phrase

Design: [`plans/RECOVERY_PHRASE.md`](plans/RECOVERY_PHRASE.md). The recovery core
is unit-tested with no Docker/network/root required:

```bash
python3 scripts/tests/test_recovery.py         # must end with "11/11 passed"
python3 scripts/tests/test_master_password.py  # must end with "14/14 passed" (needs Flask)
```

On real hardware:

**P1 — mint / store / show**
- [ ] Fresh provision → the setup success page shows a **6-word recovery phrase**
      distinct from the master password. The generated master password is itself
      a hyphen-joined word passphrase (B1).
- [ ] After clicking through (creds cleaned up), reloading does NOT re-show
      either secret.
- [ ] `grep RECOVERY_ /opt/homebrain/.env` shows `RECOVERY_SCRYPT_HASH` etc. but
      **no plaintext phrase** anywhere on disk.
- [ ] Settings → **Recovery Phrase** card shows "configured"; "Regenerate"
      reveals a new phrase once and the old one stops verifying.

**P2 — verify + dashboard-login recovery (do this from the LAN)**
- [ ] Log out. On the login gate, **Forgot your password?** reveals the recovery
      form. A wrong phrase is rejected (with a ~2s delay); the correct phrase +
      a new password returns "Recovery accepted".
- [ ] Log in with the **new** password. Old password no longer works.
- [ ] Attempt recovery over the **remote tunnel** with `RECOVERY_ALLOW_REMOTE=false`
      → refused (403). Set it `true` → allowed.
- [ ] Exceed the rate limit (>5 attempts/hour) → throttled.

**P3 — full-stack rotation** *(merge gate — verify on BOTH x86 and RPi)*
- [ ] After a recovery reset, check `/var/log/homebrain/setup.log` for
      "Master password rotation complete".
- [ ] **Nextcloud** admin login works with the new password.
- [ ] **Home Assistant** admin login works with the new password, and the log
      says "rotated **and verified**". The rotation confirms by logging in to HA
      before writing `HA_ADMIN_PASSWORD`, so a warning there means `.env` kept
      the OLD password and HA still accepts it — the two never disagree.
      (`hass --script auth` exits 0 even when it changes nothing, so the exit
      status alone proves nothing.)
- [ ] **MariaDB** root authenticates with the new password
      (`docker exec -e MYSQL_PWD=<new> <db> mariadb -u root -e 'SELECT 1'`).
- [ ] **Vault** admin-panel SSO button works (token re-derived); **OpenClaw**
      gateway reachable on a GPU box (token re-derived).
- [ ] A per-user **Vault** item created before recovery still requires that
      user's own unchanged password and still decrypts — recovery did NOT touch
      E2E vaults.
- [ ] **Abort safety:** kill `rotate_master_password.sh` after the MariaDB step
      but before completion → the box stays loginable; re-running recovery is
      idempotent and completes.

---

## Master password: deliberate change + credential download

Same rotation engine as P3 above, entered by a logged-in user who still knows
their password. Run on real hardware (it re-credentials live services).

**Change the password (Settings → Master Password)**
- [ ] Wrong current password → "Invalid current password" after a ~2s delay, and
      `/var/log/homebrain/main_setup.log` shows **no** rotation starting.
- [ ] New password containing a space or quote → rejected with the charset rule.
- [ ] Re-using the current password → "That is already your current password."
- [ ] **Suggest** fills both fields with a readable word passphrase that submits
      without a validation error.
- [ ] A valid change returns "Rotation started"; the global status banner shows
      **Master Password Rotation**, then the log ends with
      "Master password rotation complete".
- [ ] The session stays signed in throughout (no bounce to the login gate).
      Sign out afterwards → the **new** password works, the old one does not.
- [ ] Nextcloud, Home Assistant, MariaDB root, Vault admin SSO and (GPU) the
      OpenClaw gateway all accept/derive the new password — same checks as P3.
- [ ] `grep HA_ADMIN_PASSWORD /opt/homebrain/.env` matches the password Home
      Assistant actually accepts. Regression guard: the change is only recorded
      after a real HA login succeeds, so `.env` can never advertise a password
      HA never took.
- [ ] Backups → **legacy** archives written before the change are flagged as
      needing the old passphrase. Dual-wrapped (HBK1) archives are **not**
      flagged — they still open with the recovery phrase. The automatic
      post-rotation full backup appears unflagged once it finishes.
- [ ] **Abort safety:** stop the DB container, then attempt a change → it fails
      before any `.env` write and the **old** password still works everywhere.
- [ ] Start a change while another task is running → 409, nothing launched.

**Handover window (fresh provision)**
- [ ] Between "Deployment Complete" and clicking *I have saved my password*, the
      login gate accepts **either** the factory password from the device label
      **or** the freshly generated master password. Setup marks itself complete
      before the owner has read anything, so a session lost in that window (tab
      closed, install started on a phone and finished on a laptop) must not lock
      them out of the only page that ever shows the recovery phrase.
- [ ] After the credentials are claimed, the factory password stops working and
      only the master password opens the box.
- [ ] **Remote mode:** newt/cloudflared are **not** running between "Deployment
      Complete" and *I have saved my password*. After that click,
      `activate_tunnels` brings them up. Local mode has no tunnel profiles; this
      check is a no-op there. Off-hardware: `bash scripts/tests/test_handover_tunnels.sh`

**Credential download (.txt)**

Sheet text and the DOM contract are covered off-hardware — run these first, they
need no box (see `docs/plans/RECOVERY_SHEET.md` §6):

```
osascript -l JavaScript scripts/tests/test_creds_sheet.js 2>&1  # "all passed", 27 checks
python3 scripts/tests/test_creds_sheet_wiring.py                # "all passed" (needs Flask)
```

The `2>&1` is not decoration: JXA writes `console.log` to **stderr**, so piping
stdout alone shows an empty stream whether the suite passed, failed, or never
ran. (Under `node` in CI it goes to stdout as usual.)

`osascript` is not a stylistic choice — JavaScriptCore is the only JS runtime on
the dev Mac, and it is the same engine Safari runs. Both suites also run in CI
(the first under `node`), so a red build is the first signal, not this list.

On hardware, what the tests above cannot reach — that a real browser honours the
`download` attribute and writes the bytes to disk:
- [ ] Fresh provision → the setup success page's **Download as .txt** saves a
      sheet containing the master password *and* the recovery phrase, with the
      "does NOT unlock individual Vault items" note.
- [ ] Settings → Recovery Phrase → Regenerate → **Download as .txt** saves a
      sheet with the phrase only (no master password).
- [ ] Settings → Master Password → the **Download updated sheet** button appears
      only *after* the rotation reports success, not when it is launched. Force a
      failure (stop the DB container) and confirm no button appears — a sheet
      naming a password the box never took is worse than no sheet.
- [ ] That sheet carries the new password and `Recovery phrase:  unchanged` — and
      no phrase. On a box with **no** phrase configured, that line is absent
      rather than promising one.
- [ ] With `/static/creds_sheet.js` blocked (devtools request blocking), the
      handover page's download button shows a visible error instead of doing
      nothing — the next click deletes the credentials for good.
- [ ] Filename is `homebrain-recovery-<YYYY-MM-DD>T<HH>-<MM>-<SS>.txt`; two
      downloads in the same day — including two clicks of the same button
      seconds apart — do **not** collide into `…(1).txt`.
- [ ] Open the saved file in a plain text editor: no mojibake (the body is ASCII
      by contract) and the line breaks survive.
- [ ] The download works on the LAN over the self-signed HTTPS origin, and on a
      phone — iOS Safari is the weakest link for `<a download>` and setup is
      phone-friendly by design.

---

## Backup unlock (recovery phrase opens archives)

Design: [`plans/BACKUP_UNLOCK.md`](plans/BACKUP_UNLOCK.md). Off-hardware first:

```
python3 scripts/tests/test_backup_crypto.py    # must end with "11/11 passed"
python3 scripts/tests/test_recovery.py
python3 scripts/tests/test_setup_credentials.py
python3 scripts/tests/test_master_password.py
bash scripts/tests/test_backup_unlock.sh
node scripts/tests/test_creds_sheet.js
```

**On hardware (x86 and RPi)**

- [ ] Fresh provision → first full backup: `python3 src/backup_crypto.py inspect --archive <file> --field unlock` prints `master_or_phrase`. Same-box restore with an empty passphrase still works.
- [ ] Dashboard restore of that archive, passphrase = recovery phrase → data comes back.
- [ ] Change master password → pre-change HBK1 archive is **not** flagged "needs previous password"; it opens with the phrase and with the old password; the automatic new backup opens with the new password and with the phrase.
- [ ] Settings → regenerate phrase → local HBK1 archives open with the **new** phrase and fail the old one.
- [ ] Box provisioned before this ships: Settings → Recovery Phrase shows "Enable backup unlock"; after typing the phrase, the next backup is dual-wrapped; a leftover legacy archive still needs the old master password.
- [ ] Dead box: wizard Restore system with **only** the recovery phrase → handover shows a **new** master password and the **same** phrase; Dashboard / Nextcloud / HA accept the new password; files are present.
- [ ] Dead-box control: wizard with the **master** password → box comes back on that password.
- [ ] `BACKUP_ENCRYPT=false` still publishes plaintext `.tar.gz`.
- [ ] While a backup runs, nothing large appears under `/tmp`:
      `find /tmp -type f -printf '%s %p\n' | sort -rn | head -1`. `/tmp` is
      tmpfs on the RPi, so a body staged there is the archive in RAM.
- [ ] Regenerate answers in well under a second. It rewraps archives in a
      **subprocess** on purpose — gunicorn's gevent workers turn a thread into
      a greenlet that never yields on file I/O, and the request dies with
      `WORKER TIMEOUT`. Check `manager.log` for `Could not rewrap` afterwards:
      an archive sealed under a since-rotated master password is skipped by
      design and must then show as `needs previous password` in the list.
- [ ] Archive mtimes are unchanged by a regenerate (retention sorts by mtime,
      and the off-site mirror compares size+mtime).

---

## Nuclear Reset (Destructive — Run Last on Test Hardware)

**Prerequisites**
- A freshly provisioned device that has real user data:
  - At least a few files uploaded to Nextcloud
  - At least one custom entity or automation in Home Assistant
  - At least one credential in the HomeBrain Vault
  - (GPU devices) Some OpenClaw chat history or agent memory
  - An external backup drive mounted at `/mnt/backup` with at least one prior backup

**Test Matrix** (execute on both Local and Remote modes; GPU and non-GPU devices)

- [ ] Trigger with wrong master password → rejected with clear error
- [ ] Trigger with correct password but mistyped or missing phrase → rejected
- [ ] Successful trigger using **default options** (AI models wiped, runtime kept)
- [ ] After reboot:
  - Only the factory password (device label) allows login
  - The **setup wizard** ("Initialize system") appears — *not* a handover
    screen. A reset mints no credentials of its own; the wizard generates the
    master password and its recovery phrase together, exactly as on first boot.
  - `/opt/homebrain/install_creds.json` does **not** exist
  - `docker ps -a` is **empty**. Profile-gated containers (`newt`,
    `cloudflared`, `proton-bridge`) must be gone too — a plain `compose down`
    ignores inactive profiles, and a surviving `newt` keeps publishing the
    wiped, password-less box to its tunnel
  - No old Nextcloud files, no old HA config, empty Vault, no `.openclaw` workspace remain
  - AI models directory is gone (on GPU devices)
  - External backup drive is still mounted and previous archives are readable
- [ ] Repeat with "Also delete AI runtime binaries" checked → after reset the `ai-runtime/` directory is empty (forces reinstall on next AI enable)
- [ ] Power-loss / kill simulation: interrupt the nuclear script (or pull power) mid-run, reboot, re-trigger nuclear reset → second run completes cleanly without leaving the device in a broken state
- [ ] Rate-limit verification: second attempt within 10 minutes is rejected with a clear message
- [ ] Old browser sessions/cookies are fully invalidated after the reset + reboot (trying to use an old session returns 401 and redirects to login)
- [ ] `journalctl -u homebrain-manager` and `/var/log/homebrain/manager.log` contain a clear "NUCLEAR RESET INITIATED" banner with timestamp

All items in this section must pass on real hardware before the PR is eligible for merge.

---

## Update / downgrade guard

`update.sh` refuses to move the stack backwards (Nextcloud cannot start on an
older image once its data has migrated; a half-applied manager downgrade 500s
with `'platform' is undefined`). The decision logic lives in `common.sh` and is
unit-tested with no Docker/network/root required:

```bash
bash scripts/tests/test_update_guard.sh   # must end with "failed: 0"
```

On real hardware, verify read-only (no update is executed):

- [ ] Inspect current state: `cat /opt/homebrain/version.json` and
      `grep -Eo 'nextcloud:[0-9]+\.[0-9]+\.[0-9]+' /opt/homebrain/docker-compose.yml`
- [ ] **Downgrade is blocked:** from a beta/dev install, trigger a `stable`
      update to an older tag (e.g. `sudo bash scripts/update.sh stable v1.1.0`).
      It must abort *before* rsync with "Refusing downgrade: …" and exit 1; the
      dashboard and Nextcloud stay untouched.
- [ ] **Forward/equal still works:** a normal `beta`/`dev` → `main` update (or
      `stable` → newer tag) proceeds as before and the stack comes back healthy.
- [ ] **Override path:** `sudo ALLOW_DOWNGRADE=1 bash scripts/update.sh stable v1.1.0`
      logs the override warning and proceeds (only with a backup in hand).

---

## Sign-off checklist

Complete before merging to `main`:

- [ ] All applicable sections above pass on the target hardware
- [ ] No new errors in `/var/log/homebrain/` or `journalctl -u homebrain-manager`
- [ ] `docker compose ps` shows all expected services healthy after a full stack restart
- [ ] Tested both fresh-install and re-run (idempotency) where relevant
- [ ] PR description notes which sections were tested and on which variant/configuration
