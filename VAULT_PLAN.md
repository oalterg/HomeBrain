# HomeBrain Vault — Design & Implementation Plan

A self-hosted personal password manager + secure document vault, integrated as a first-class HomeBrain service on the same single-machine, master-password, Pangolin-tunnelled model as Nextcloud / Home Assistant / OpenClaw.

---

## 1. Goals & Non-Goals

### Goals
- A **personal / family-grade password manager** running entirely on the HomeBrain box.
- Full compatibility with mainstream Bitwarden clients (browser extensions, iOS, Android, desktop, CLI).
- **Zero new credentials to memorise** — gated by the existing `MASTER_PASSWORD` for admin operations; user vault password is set on first use.
- **Reachable both modes**: LAN-only (`vault.homebrain.local`) and Pangolin remote (`vault.<tunnel-domain>`).
- **Backed up by default** — vault data and DB folded into the existing nightly `backup.sh` archive.
- **Document vault**: encrypted file storage for IDs, scans, recovery codes, etc. — both small (per-credential attachments inside the vault) and large (a dedicated Nextcloud group folder marked private/end-to-end-encrypted).
- Runs on **both HomeBrain (x86 + GPU) and HomeCloud (RPi / no-GPU)** editions — not gated by `HAS_GPU`.

### Non-Goals
- Multi-tenant / org-wide deployment with SSO, audit logs, SCIM provisioning. (Vaultwarden supports this; we just don't surface it in the dashboard for v1.)
- A custom UI. We ship the upstream Bitwarden web vault as served by Vaultwarden — no fork, no reskin.
- Replacing Nextcloud as the primary file-sync target. Documents live in Nextcloud; the vault stores **secrets and small attachments**.

---

## 2. Software Choice — Vaultwarden

Surveyed three serious self-hostable options:

| | Vaultwarden | Bitwarden self-hosted | Passbolt |
|---|---|---|---|
| Stack | Single Rust binary | ~10 containers, MS SQL | LAMP + GPG keys |
| Idle RAM | **~50 MB** | 2 GB+ | ~500 MB |
| Clients | All official Bitwarden apps | Same | Custom only |
| Premium features (TOTP, attachments, Send) | **Free** | License-gated | N/A |
| Audit history | Community project, no formal audit | Yes, audited | Yes, audited |
| Fits HomeBrain footprint | **Yes** | No (RAM) | Heavy + non-standard auth |

**Decision: Vaultwarden.** Single Docker container, drops into the existing compose file, reuses the existing MariaDB. The Bitwarden client ecosystem is the long pole — Passbolt's GPG model would not let us point a phone at the App Store and have it just work.

Pinned in `config/versions.json` exactly the way `llama_cpp.tag` is pinned today, so updates flow through the dashboard's "Update" button.

---

## 3. Architecture

```
HomeBrain
├── nextcloud      (Docker, MariaDB-backed)
├── homeassistant  (Docker)
├── vaultwarden    (Docker, MariaDB-backed)   ← NEW
├── db (MariaDB)   (Docker, shared)
├── newt           (Docker, optional, Pangolin)
└── llama-server / whisper-server / openclaw  (systemd, GPU only)
```

### Data flow
- Browser/app → Pangolin tunnel (`vault.<domain>`) **or** LAN HTTPS (`https://homebrain.local:8443`) → reverse proxy → `vaultwarden:80`.
- Vaultwarden persists ciphers in MariaDB (`vaultwarden` DB, dedicated user).
- Attachments, Send blobs, icons, and the `rsa_key.*` JWT-signing keys live on disk at `/home/homebrain/vault-data`.

### Why MariaDB (not SQLite)
- The existing `db` service is already healthchecked, backed up, and tuned. Reusing it gives us point-in-time `mysqldump` snapshots for free, and avoids a second backup path.
- Vaultwarden's MySQL/MariaDB backend is first-class (not an afterthought) and is what their docs recommend for production.
- A separate MariaDB **user** (`vaultwarden_user`) with grants only on the `vaultwarden` DB keeps blast radius contained if a Nextcloud bug ever leaks DB creds.

### HTTPS
- **Remote mode**: Pangolin terminates TLS at the edge. New tunnel resource `vault.<PANGOLIN_DOMAIN>` → `vaultwarden:80`. Mirrors how Nextcloud and HA are exposed today.
- **Local mode**: Bitwarden mobile/desktop clients **refuse plain HTTP**. Two-track plan:
  1. v1 ships an **automatic mkcert-style local CA + Caddy reverse proxy** at `https://homebrain.local:8443/vault` — the dashboard exposes the CA cert as a download so users install it on their phone once.
  2. The browser-based web vault works over plain HTTP on LAN as a fallback for the squeamish, restricted to RFC1918 + `homebrain.local` origins via Vaultwarden's `DOMAIN`/`SIGNUPS_ALLOWED` settings.
- WebSocket live-sync uses Vaultwarden's built-in WS endpoint on `:80` (1.29+ — no separate 3012 port).

### Master-password integration
Same pattern as the existing OpenClaw gateway token (`MASTER_PASSWORD` derives a stable secret).

- `ADMIN_TOKEN` is the **Argon2id hash** of `MASTER_PASSWORD` salt-mixed with a per-install nonce written once by `provision.sh`. The plain-text token is never stored on disk.
- The dashboard backend can reconstruct the plain admin token on demand (it has access to `MASTER_PASSWORD` and the nonce), so the **Vault admin panel link in the dashboard auto-authenticates** the user, exactly like the OpenClaw "Open Dashboard" button.
- Vault user accounts (per-family-member) have their **own** master passwords set on first login. The admin password resets / disables them but cannot decrypt their vaults — same end-to-end model as Bitwarden.

---

## 4. Files to add / change

### New
- `docker-compose.yml` — adds `vaultwarden` service block, depends_on `db`.
- `config/versions.json` — adds `vaultwarden.tag` (start with current pinned: `1.35.7`).
- `config/.env.template` — adds `VAULT_*` block (DB user/pass, admin nonce, public URL).
- `scripts/provision_vault.sh` — sourced by `provision.sh`. Creates DB + user, generates nonce, derives admin token, creates `/home/homebrain/vault-data`, opens Pangolin route on remote mode.
- `src/templates/_vault_card.html` — dashboard tile (status pill, "Open Vault", "Open Admin Panel", attachment-quota, last-backup).
- `src/app.py` — `/api/vault/status`, `/api/vault/admin-link` (returns a one-shot signed URL into the admin panel), `/api/vault/bootstrap` (creates first user via admin API, then disables signups).
- `src/app.py` — `/api/logs/vaultwarden` whitelisted in the existing log viewer.

### Changed
- `scripts/backup.sh` — new section between OpenClaw and "Restart Services":
  1. `docker stop vaultwarden` (clean shutdown for attachment / icon consistency).
  2. `mysqldump` of the `vaultwarden` DB into `staging/vault_db/vaultwarden.sql`.
  3. `rsync` `/home/homebrain/vault-data` → `staging/vault_data/`. Highlight `rsa_key.pem` / `rsa_key.pub.pem` — losing these invalidates every active session.
  4. `docker start vaultwarden`.
- `scripts/restore.sh` — symmetric restore.
- `scripts/update.sh` — bumps the `vaultwarden.tag` pin via the same flow as llama.cpp.
- `scripts/redeploy_tunnels.sh` — adds the `vault.*` resource on remote mode.
- `src/templates/dashboard.html` — embeds the new card; adds Vault to the service-status grid.
- `README.md` — new "Vault" feature row + screenshot; `ROADMAP.md` — move from planned → shipped on release.
- `TESTING.md` — new E2E section (10 steps, see §7).

### Untouched
- `scripts/common.sh` already exports `HOMEBRAIN_HOME`, `MYSQL_*`, `HAS_GPU` — vault provisioning reads these.
- `src/migration.py` — no changes; vault doesn't share schema with the dashboard's own state.

---

## 5. Docker-compose service block (sketch)

```yaml
  vaultwarden:
    image: vaultwarden/server:${VAULTWARDEN_TAG:-1.35.7}
    restart: unless-stopped
    ports:
      - "127.0.0.1:${VAULT_PORT:-8082}:80"
    volumes:
      - ${VAULT_DATA_DIR:-/home/homebrain/vault-data}:/data
    environment:
      - DOMAIN=${VAULT_DOMAIN}                 # e.g. https://vault.cloud.example.com
      - DATABASE_URL=mysql://${VAULT_DB_USER}:${VAULT_DB_PASSWORD}@db/${VAULT_DB_NAME}
      - ADMIN_TOKEN=${VAULT_ADMIN_TOKEN}       # argon2id hash, derived in provision_vault.sh
      - SIGNUPS_ALLOWED=${VAULT_SIGNUPS_ALLOWED:-false}
      - SIGNUPS_VERIFY=true
      - INVITATIONS_ALLOWED=true
      - WEBSOCKET_ENABLED=true
      - PUSH_ENABLED=false                     # opt-in later via the dashboard
      - SENDS_ALLOWED=true
      - EMERGENCY_ACCESS_ALLOWED=true
      - LOG_LEVEL=warn
      - EXTENDED_LOGGING=true
      - SHOW_PASSWORD_HINT=false
      - ROCKET_WORKERS=2
    depends_on:
      db:
        condition: service_healthy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--spider", "http://localhost/alive"]
      interval: 30s
      timeout: 5s
      retries: 5
```

`DOMAIN`, `VAULT_PORT`, `VAULT_SIGNUPS_ALLOWED` are set by the dashboard at runtime (deployment mode + first-user bootstrap state).

---

## 6. Dashboard UX

### Service card (LAN-mode wireframe)
```
┌─ Vault ───────────────────────────── ●  online ─┐
│  vault.homebrain.local                          │
│  3 users · 47 items · 12 MB attachments         │
│  Last backup: today 03:00                       │
│                                                 │
│  [ Open Vault ]   [ Admin panel ]   [ Logs ]    │
└─────────────────────────────────────────────────┘
```
- **Open Vault** → drops the user on the upstream Bitwarden web vault.
- **Admin panel** → goes through `/api/vault/admin-link`, which signs a short-lived `?token=…` query and 302s to `/admin`. Mirrors how OpenClaw's "Open Dashboard" button works.
- **First-run modal** (when `users_total == 0`) — collects an email + sets a master password, calls Vaultwarden's admin invite API, then flips `SIGNUPS_ALLOWED=false` and restarts the container.

### Setup wizard
After the existing "deployment mode" step, add a new step: **"Enable HomeBrain Vault?"** (default yes). If the user opts out, the service is still installed but stopped, and the card is hidden behind a toggle in Settings → Services. Avoids forcing a password manager on users who already use 1Password / iCloud Keychain.

---

## 7. Test plan (additions to `TESTING.md`)

E2E on the production target (`homebrain@192.168.178.58`):

1. Fresh provision → vault container comes up healthy, dashboard card shows "online".
2. First-run modal creates user `me@home`, signups auto-disable, container restarts, card shows "1 user".
3. Bitwarden Android app — point at `https://vault.<tunnel>` → log in → add credential → log out → log in on Firefox extension → credential syncs in <2 s.
4. Add a 2-MB PDF as an attachment to a credential → verify file lands under `/home/homebrain/vault-data/attachments/` and decrypts on a second client.
5. Use Send to share a one-time secret → verify expiry honoured server-side.
6. Trigger `backup.sh` mid-session → archive contains `vault_db/vaultwarden.sql` and `vault_data/rsa_key.pem`. Existing logged-in sessions survive (no `rsa_key` rotation).
7. Wipe `/home/homebrain/vault-data` and restore from archive → all clients still authenticate, all attachments decrypt.
8. Bump `vaultwarden.tag` in `versions.json` → click Update → container recreated, no data loss, sessions still valid.
9. Switch deployment mode `local` ↔ `remote` → `DOMAIN` env var updates, redeploy script adjusts the Pangolin resource, clients re-resolve.
10. Stop Vaultwarden manually → dashboard card flips to "stopped", systemd unit auto-restarts within 30 s (existing crash-loop protection covers this).

---

## 8. Document vault — secure file storage layer

Two complementary stores, picked by file size:

| | Vault attachment | Nextcloud "Documents (Encrypted)" |
|---|---|---|
| Size | up to ~10 MB / file (configurable) | unbounded |
| Where | inline on a credential | dedicated NC group folder |
| Encryption | E2E (Bitwarden) | server-side AES-256, optional E2EE app |
| Use case | recovery codes, ID scans, license keys | passport scans, tax PDFs, large secret docs |

Implementation:
- v1 ships only the **Vaultwarden attachment path** (it's free with the service).
- v1.1 adds a one-click "Create encrypted documents folder" button on the Vault card. It enables the upstream **Nextcloud End-to-End Encryption** app via `occ`, creates `/Documents (Encrypted)` for the master user, and surfaces the folder URL on the Vault card. No new server software — just a guided NC config.

---

## 9. Security checklist (must hold before merge)

- [ ] `ADMIN_TOKEN` is Argon2id-hashed; plain token never written to `.env` or compose file.
- [ ] `db` user `vaultwarden_user` has grants **only** on the `vaultwarden` DB.
- [ ] `rsa_key.*` permissions: `0600`, owned by container UID; backup preserves mode.
- [ ] Pangolin tunnel resource (remote) restricts methods / sets `Strict-Transport-Security` and `Referrer-Policy` headers.
- [ ] LAN reverse proxy (Caddy) auto-issues a local cert; cert download endpoint requires master-password auth.
- [ ] `SIGNUPS_ALLOWED=false` is enforced after bootstrap by `/api/vault/bootstrap` and by a startup-time check in `app.py` that flips it back if anyone hand-edited the `.env`.
- [ ] Existing login rate-limit middleware extended to `/api/vault/*` admin endpoints.
- [ ] No `shell=True` in any vault provisioning subprocess (matches CLAUDE.md invariant).
- [ ] Backup archive containing `vaultwarden.sql` is gzip-only on the encrypted backup drive — same trust boundary as Nextcloud's DB dump today.

---

## 10. Phasing & exit criteria

| Phase | Scope | Exit |
|---|---|---|
| **P1 — Service + tunnel** | compose service, MariaDB DB+user, Pangolin route, dashboard tile, manual admin-token entry | Bitwarden iOS app logs in via tunnel, syncs a credential. |
| **P2 — Bootstrap UX** | First-run modal, signup gating, derived admin token, "Open Admin" SSO button | Fresh provision → user is logged into a usable vault in <90 s, no manual env editing. |
| **P3 — Backup / restore** | backup.sh + restore.sh integration, retention parity, log-viewer hookup | TESTING.md steps 6 + 7 pass on real hardware. |
| **P4 — Local-mode HTTPS** | Caddy reverse-proxy with mkcert-style local CA, cert download endpoint | Bitwarden Android connects to `https://homebrain.local:8443` after a single CA install. |
| **P5 — Document vault** | NC E2EE app enablement, "Documents (Encrypted)" folder, Vault-card surfacing | One-click setup creates a folder visible in the Nextcloud client and refuses unencrypted access. |
| **P6 — AI integration (stretch)** | OpenClaw MCP tool: `vault.search(query) → summary`, gated by per-session unlock prompt | "Hey OpenClaw, what's my router admin password?" answers from the vault, returns nothing if vault is locked. |

P1–P3 are the MVP and gate the merge to `main`. P4–P6 ship behind feature flags in subsequent PRs.

---

## 11. Open questions

1. **HTTPS in LAN mode** — ship a local CA installer, or require the user to use remote mode if they want mobile clients? Local-CA is friendlier but adds cross-platform install instructions. Recommendation: ship it, but document remote-mode as the path of least resistance.
2. **Push notifications** — Bitwarden's official push service is centralised. Self-hosted push is non-trivial. Default `PUSH_ENABLED=false`; mobile clients fall back to polling.
3. **Family member onboarding flow** — admin-side invite emails require an SMTP relay. Either (a) wire the existing Nextcloud SMTP creds, or (b) generate offline invite links the admin shares manually. v1 picks (b); v1.1 reuses NC SMTP if configured.
4. **Where the attachments live** — currently `/home/homebrain/vault-data`. Should this move under the existing backup drive once mounted, to remove the OS-disk hop? Decision deferred to a measurement of typical attachment volume.

---

## 12. Sources

- [Vaultwarden self-hosting guide 2026](https://aicybr.com/blog/vaultwarden-complete-self-hosting-guide)
- [Argon2id admin-token best practice](https://deployn.de/en/blog/setup-vaultwarden/)
- [Passbolt vs Vaultwarden vs Bitwarden 2026 (OSSAlt)](https://ossalt.com/guides/passbolt-vs-vaultwarden-vs-bitwarden-teams-2026)
- [Vaultwarden vs Bitwarden self-hosted resource comparison](https://selfhosting.sh/compare/bitwarden-vs-vaultwarden/)
- [Vault Management API (DeepWiki, dani-garcia/vaultwarden)](https://deepwiki.com/dani-garcia/vaultwarden/3.3-vault-management-api)
- [File attachments in Vaultwarden / Bitwarden](https://bitwarden.com/help/attachments/)
