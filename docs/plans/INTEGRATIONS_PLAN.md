# OpenClaw × HomeBrain — Holistic Integration Plan

OpenClaw is the *only* surface most users will ever touch — WhatsApp message → answer or action.
For that surface to feel like a real personal AI, it needs first-class, out-of-the-box access to:

- **Home Assistant** (already works via long-lived access token; productionise it through MCP)
- **Nextcloud** (files, notes, calendar, contacts, Talk)
- **HomeBrain Vault** (Vaultwarden — MCP wrapper exists in `scripts/mcp-vault.py`)
- **Email** (Protonmail via Bridge, plus generic IMAP/SMTP for everyone else)
- *(near-term extension)* Calendar, Contacts, Reminders, Notes — surfaced via the same integrations

This document is the lead-engineer-level plan: principles → architecture → concrete files-to-touch → phasing.

---

## 1. First Principles

> **Everything OpenClaw does to the rest of the box is an MCP call. No bespoke per-service shims.**

1. **One protocol — MCP.** Every integration is an MCP stdio server registered in `~/.openclaw/openclaw.json` under `mcp.servers[]`. The agent has exactly one mental model for "talk to a system": list tools, call tool, handle envelope. This is already the model used by `scripts/mcp-vault.py`; we extend it to HA, NC, and Email.
2. **Single root of identity — `MASTER_PASSWORD`.** The dashboard derives every downstream credential (HA LLAT, NC app password, Vault admin token, Email-account encryption key) from it, exactly as `VAULT_PLAN.md §3` already does for Vault. The agent never sees raw credentials; it only sees signed *handles* enforced by the MCP servers themselves.
3. **Capability tiers, not boolean access.** Each MCP exposes three classes of tools, never more:
   - **Read** (`*.search`, `*.list`, `*.get_metadata`) — no consent prompt. Always returns metadata only, never raw secrets / full bodies / large attachments.
   - **Act** (`*.send`, `*.set_state`, `*.create_event`, `*.delete_*`) — requires a per-action consent stub from the agent (more on the WhatsApp consent loop below) and is rate-limited.
   - **Reveal** (`vault.reveal`, `email.fetch_full_body`, `nc.download_attachment`) — explicit user intent, every call audited to `/var/log/homebrain/mcp-*-audit.log`.
4. **Standardised failure envelope.** Every tool returns `{ok, locked?, unavailable?, error?, hint?, ...payload}`. The agent already handles `unlocked: false` for Vault; we generalise the shape so HA-down or IMAP-stalled degrade identically.
5. **Zero manual JSON editing.** The dashboard owns `openclaw.json`. Each integration has a card with **Connect / Disconnect / Test / Logs**, mirroring the Vault card. If users hand-edit the file, a startup check in `app.py` reconciles and warns.
6. **Privacy by default.** Read tools redact: emails return `from`, `subject`, `received`, never bodies until asked. Vault never returns passwords from `search`. NC `files.list` returns paths + sizes, not contents. The LLM sees what a curious housemate would see, not what the safe holds.
7. **WhatsApp-native consent.** The user is not at a dashboard when they ask the agent to do things. Confirmation can't depend on a browser pop-up — see §6.

---

## 2. Target State (one diagram)

```
                ┌──────────────────────── HomeBrain box ──────────────────────────┐
                │                                                                 │
   WhatsApp ◀──▶│  OpenClaw daemon (systemd, GPU)                                 │
                │     │                                                           │
                │     ├─stdio─ mcp-homeassistant   ─http─▶ HA :8123 (LLAT)        │
                │     ├─stdio─ mcp-nextcloud       ─http─▶ Nextcloud :80          │
                │     ├─stdio─ mcp-vault           ─exec─▶ bw → Vaultwarden       │
                │     ├─stdio─ mcp-email           ─imap─▶ Proton Bridge :12143   │
                │     │                            │       OR direct IMAP/SMTP   │
                │     └─stdio─ mcp-homebrain       ─http─▶ Flask dashboard API   │
                │                (system-self tool: backups, status, restart)    │
                │                                                                 │
                │  Flask dashboard (homebrain-manager.service)                    │
                │     • owns openclaw.json mcp.servers[]                          │
                │     • per-integration cards: Connect / Test / Logs              │
                │     • derives all per-service credentials from MASTER_PASSWORD  │
                └─────────────────────────────────────────────────────────────────┘
```

Five MCP servers, all stdio subprocesses of the OpenClaw daemon. No new ports exposed to the LAN beyond what already exists.

---

## 3. Per-integration design

### 3.1 Home Assistant

**Status quo.** A long-lived access token (LLAT) generated in the HA UI, pasted into something — but the wiring is ad-hoc and the user owns the token lifecycle. Works "quite well" today but is the integration most likely to break on a re-provision.

**Target.**
- **Server:** Home Assistant has an *official* `mcp_server` integration (HA core 2025.x+). It's a first-party HA add-on that exposes the `Assist` API as MCP tools. We use it as the canonical path; we ship a thin `mcp-homeassistant.py` shim only as the fallback for users on older HA versions or who prefer raw REST.
- **Auth bootstrapping:** Setup wizard adds a step **"Link Home Assistant"** with two paths:
  1. *Auto* — dashboard calls `POST /auth/long_lived_access_token` against the HA container using the bootstrap admin credentials it already created in `provision.sh`. Token is stored in `/opt/homebrain/.env` as `HA_LLAT=...` (mode 0600), with a copy in `~/.openclaw/ha.token` for the MCP server to read.
  2. *Manual* — paste a token. Same storage, same MCP wiring.
- **Tool surface (read):** `ha.entity_search(query)`, `ha.area_list()`, `ha.state(entity_id)`, `ha.history(entity_id, since)`. Metadata + numeric state only.
- **Tool surface (act):** `ha.call_service(domain, service, target, data)` with a domain allowlist (lights, switches, climate, media_player, scripts, automations). Critical domains (`recorder`, `system_log`, `homeassistant.restart`) are denied at the MCP layer.
- **Tool surface (reveal):** none — HA has no secret-reveal class.
- **Failure envelope:** `unavailable: true` + `hint: "Home Assistant not reachable on http://homeassistant:8123"` when the upstream is down; agent can reply "the smart-home brain is offline, want me to try restarting it?" → that's a self-tool (`mcp-homebrain.service.restart`) call.

### 3.2 Nextcloud

**Choice.** `cbcoutinho/nextcloud-mcp-server` (Python, MIT, actively maintained, broadest surface). Falls back to `No-Smoke/nextcloud-mcp-comprehensive` if upstream regresses. Both authenticate via **Nextcloud app password** — *never* the master NC password, *never* OAuth (which is still flagged experimental in 2026).

**Bootstrap.**
- After NC is up (already happens in `provision.sh`), dashboard calls `occ user:add-app-password homebrain-openclaw` and stores the result in `~/.openclaw/nextcloud.token` + `/opt/homebrain/.env`.
- App-password scope is restricted in `occ` to the apps we use (`dav`, `files`, `notes`, `calendar`, `contacts`, `talk`). A leak of this token cannot reset the user's NC password or change billing — same containment principle as `vaultwarden_user`'s grant scope.

**Tool surface.**
- **Read:** `nc.files_list(path)`, `nc.files_search(query)`, `nc.notes_list`, `nc.notes_get(id)`, `nc.calendar_events(range)`, `nc.contacts_search`, `nc.talk_rooms`, `nc.talk_messages(room, since)`.
- **Act:** `nc.files_upload`, `nc.files_move`, `nc.files_share(path, expiry?)`, `nc.notes_create`, `nc.notes_update`, `nc.calendar_create_event`, `nc.contacts_create`, `nc.talk_send(room, body)`. Each `act` call writes one line to `/var/log/homebrain/mcp-nextcloud-audit.log`.
- **Reveal:** `nc.files_download(path, max_bytes=2_000_000)` — capped, audited. Larger files require a one-shot share link the user opens themselves (the agent gets the URL, not the bytes).

**Privacy.** `files_list` returns paths and sizes only. `files_search` returns paths matching a query, not contents. The LLM doesn't ingest document text unless the user explicitly says "summarise that PDF".

### 3.3 Vault

Already shipped (`scripts/mcp-vault.py`, `config/openclaw-mcp-vault.example.json`). Plan-level changes:

- Promote the *example* MCP config to *applied-by-default*. The dashboard's existing Vault card writes the entry into `openclaw.json` automatically when the user clicks "Connect to OpenClaw" (currently the user does this manually per `VAULT_PLAN.md §10 P6`).
- Add `vault.list_folders` and `vault.create_login(name, username, password, uri)` so the agent can offer **"save this to your vault"** when the user pastes a credential into a chat. This needs the same WhatsApp-confirm consent flow as any `act`-tier call.
- Tighten `vault.reveal` audit: include WhatsApp message-id + chat-id of the request, so the audit log is forensically useful.

### 3.4 Email — Protonmail and beyond

**Reality check.** "Just give the agent my email" is the highest-leverage feature here, and the most dangerous. Reading email is innocuous; *sending* email from the user's identity is one of the most impactful things a personal AI can do. The plan treats email as a tier-1 integration with tier-3 caution.

**Architecture — two layers, separable:**

```
mcp-email (single MCP server, generic IMAP/SMTP)
    │
    ├──▶ Protonmail Bridge container (only for Proton users)
    │       — exposes localhost:12143 IMAP, :12025 SMTP
    │       — credentials managed by the Bridge, NOT by HomeBrain
    │
    └──▶ Direct IMAP/SMTP for any other provider (Gmail w/ app password,
         iCloud w/ app-specific password, Fastmail, generic IMAP host)
```

**Why this split.**
- Protonmail does not speak IMAP directly — Bridge is mandatory for Proton users.
- Bridge is heavy enough (and Proton-account-specific) that we don't want every HomeBrain to run it. Make it a profile-gated docker-compose service (`profiles: [proton-bridge]`), enabled only when the user adds a Proton account in the dashboard.
- The MCP server is a *single binary regardless of provider*: it talks IMAP/SMTP to localhost:12143/12025 (Bridge) or to `imap.gmail.com:993` (direct), per stored account config.

**Server choice.** `ai-zerolab/mcp-email-server` is the closest match in 2026 — IMAP+SMTP, multi-account, MIT, Python (slots into our existing venv). We pin it in `config/versions.json` like everything else.

**Tool surface.**
- **Read:** `email.list_unread(account, limit=20)` → `[{from, subject, received, has_attachments, id}]`. `email.search(account, query)`. `email.thread(thread_id)` returns subject tree, no bodies.
- **Reveal:** `email.fetch(id, format="text")` — full body. Audited. WhatsApp consent before the agent is allowed to read this aloud over a voice reply (don't summarise a 2FA code into the LM's working set unless the user said so).
- **Act:** `email.draft(account, to, subject, body)` — creates a draft in the account's Drafts folder, returns the draft id and a one-tap dashboard link to send. **The agent cannot directly send.** This is a deliberate choice: drafts are reversible, sends are not. Power users can opt into `email.send_direct` via a Settings toggle that requires re-entering the master password.
- **Act:** `email.archive(id)`, `email.flag(id)`, `email.move(id, folder)` — these are reversible-enough to allow without dashboard hop, with the WhatsApp consent stub.

**Bootstrap.**
- Dashboard adds a **"Email accounts"** section: card per account, add/remove flow.
- For Proton: collect Bridge bridge-pass once, store under `/home/homebrain/.email/proton.bridge.json` (mode 0600, encrypted at rest with a per-install key derived from `MASTER_PASSWORD`).
- For IMAP/SMTP: collect host, port, app-password. Same storage path, same encryption.

**Provisioning.** `scripts/provision_email.sh` (new, sourced from `provision.sh` only when at least one account is configured): pulls the Proton Bridge image, creates `/home/homebrain/.email/`, wires the MCP server into `openclaw.json`.

### 3.5 The "self" MCP — `mcp-homebrain`

A small server that exposes the dashboard's own API to OpenClaw. This is what lets the agent answer **"are backups working?"** or **"please update llama.cpp"** without leaving WhatsApp.

- Auth: shared-secret derived from `MASTER_PASSWORD`, same pattern as the OpenClaw gateway token.
- Tools: `homebrain.service_status`, `homebrain.service_restart(name)`, `homebrain.backup_now()`, `homebrain.update_check()`, `homebrain.gpu_stats()`, `homebrain.logs_tail(service, lines)`.
- All `act` tools pass through the same WhatsApp consent loop.

This is genuinely new code. The first three integrations are wrappers around third-party servers; this one is HomeBrain-specific and is what makes the agent a true *operator* of the box rather than a guest of it.

---

## 4. The WhatsApp consent loop

The hardest UX problem in this plan. The agent is in WhatsApp; the user is not on a browser. Every `act`-tier call needs a confirm step that lives in WhatsApp itself.

**Pattern.**

1. Agent decides to call an `act` tool. The MCP server *does not execute*; it returns `{ok: false, requires_confirmation: true, action_id: "uuid", summary: "Send email to dad@example.com: 'Heading home in 20'"}`.
2. The agent posts this summary to the WhatsApp chat as: *"⚠️ Confirm: send email to dad@example.com: 'Heading home in 20'? Reply **YES** in 60s, or open dashboard."*
3. The user replies `YES` (or anything else). OpenClaw's WhatsApp adapter recognises the magic word + recent `action_id` and re-calls the MCP tool with `confirmation_token=action_id`.
4. The MCP tool verifies the token (single-use, 60s TTL, scoped to chat-id) and executes.

**Implementation hooks.**
- New module `scripts/mcp_consent.py` — shared library imported by every `act`-capable MCP server. Stores pending confirmations in `~/.openclaw/pending_actions.json` (mode 0600), TTL-pruned on every read.
- WhatsApp adapter changes live in OpenClaw, not HomeBrain; we only need to document the contract and ship the MCP-side library.
- **Critical-class actions** (Vault reveal, email send, HA `homeassistant.restart`) require **dashboard-side confirmation** instead — the WhatsApp message contains a one-shot URL the user opens. Keeps the most dangerous actions out of pure-text confirm.

**Why not OAuth-style consent screens?** Because the user is on a phone in another room. The whole product premise is "ask the assistant, get the answer". A 4-tap consent flow defeats that. WhatsApp-text-confirm is the thinnest viable safety net.

---

## 5. Files to add / change

### New
- `scripts/mcp-homeassistant.py` — fallback HA MCP for users not on HA core ≥ 2025.x. Uses `HA_LLAT` from env.
- `scripts/mcp-nextcloud.py` — thin wrapper around `cbcoutinho/nextcloud-mcp-server` (or vendored copy pinned in `config/versions.json`). Reads app password from `~/.openclaw/nextcloud.token`.
- `scripts/mcp-email.py` — wrapper around `ai-zerolab/mcp-email-server`, multi-account.
- `scripts/mcp-homebrain.py` — self-tool MCP. Calls back into the Flask app over Unix socket.
- `scripts/mcp_consent.py` — shared confirmation library.
- `scripts/provision_email.sh` — sourced from `provision.sh` when email is configured.
- `config/openclaw-mcp.template.json` — the *default* `mcp.servers[]` block, written by the dashboard at provision time. Replaces the current `openclaw-mcp-vault.example.json` with a fuller default.
- `src/templates/_integrations_card.html` — single dashboard card per integration: status / Connect / Test / Logs.
- `src/integrations.py` — Flask blueprint owning `/api/integrations/<name>/connect|test|disconnect|logs`. One module, one set of helpers, all integrations behind it.

### Changed
- `src/app.py` — register `integrations` blueprint; add `/api/openclaw/sync_mcp_config` that rewrites `openclaw.json mcp.servers[]` from the live integration set.
- `src/templates/dashboard.html` — embed integration cards in a new "Connections" section, beneath the existing service grid.
- `scripts/provision.sh` — at the very end, after all services are up, call `python3 -m src.integrations bootstrap` to write the default `openclaw.json mcp.servers[]` block (vault + ha + nc, email skipped until user adds an account).
- `scripts/backup.sh` — back up `~/.openclaw/*.token`, `~/.openclaw/.email/`, and the per-MCP audit logs (last 30 days only — bound the size).
- `scripts/restore.sh` — symmetric.
- `docker-compose.yml` — add `proton-bridge` service block under `profiles: [proton-bridge]`.
- `config/.env.template` — add `HA_LLAT=`, `NC_APP_PASSWORD=`, `EMAIL_ENCRYPTION_KEY=` slots.
- `ROADMAP.md` — promote "MCP servers" line from medium-term to in-progress; this plan supersedes it.
- `TESTING.md` — new E2E section per integration (see §7).

### Untouched
- `scripts/common.sh` — `HOMEBRAIN_HOME`, `MASTER_PASSWORD`, `HAS_GPU` already exported.
- `scripts/mcp-vault.py` — keep as-is. It's the reference implementation; the new servers copy its envelope conventions and audit pattern verbatim.

---

## 6. Dashboard UX (Connections page)

```
┌─ Connections ─────────────────────────────────────────────────────────┐
│                                                                       │
│  🏠 Home Assistant         ●  online       [ Test ] [ Disconnect ]    │
│     12 areas · 84 entities · last sync 3 s ago                        │
│                                                                       │
│  ☁️  Nextcloud              ●  online       [ Test ] [ Disconnect ]    │
│     7 GB used · Notes, Calendar, Contacts, Talk enabled               │
│                                                                       │
│  🔐 HomeBrain Vault         ●  unlocked    [ Lock ]  [ Logs ]         │
│     47 items · 2 reveals last 24h                                     │
│                                                                       │
│  ✉️  Email                                                             │
│     • me@protonmail.com    ●  bridge running                          │
│     • work@gmail.com       ●  imap.gmail.com:993                      │
│     [ + Add account ]                                                 │
│                                                                       │
│  All integrations expose tools to OpenClaw via MCP.                   │
│  [ View raw mcp.servers config ]   [ Audit logs ]                     │
└───────────────────────────────────────────────────────────────────────┘
```

Each row is the same `_integrations_card.html` partial. Status pill is driven by a per-integration health check that hits the MCP server's `tools/list` endpoint — if it answers, it's green.

---

## 7. Test plan additions (TESTING.md)

E2E on `homebrain@192.168.178.58`. One block per integration; each must pass before merging.

**Home Assistant**
1. Fresh provision → HA card "online", `ha.entity_search("kitchen")` over a curl-driven OpenClaw test returns ≥1 result.
2. `ha.call_service("light", "turn_on", {entity_id:"light.kitchen"})` toggles a real bulb on the test rig.
3. `homeassistant.restart` is denied by the MCP-side allowlist.

**Nextcloud**
4. App password is created via `occ` and written to `~/.openclaw/nextcloud.token` mode 0600.
5. `nc.files_list("/")` returns the user's top-level folders. `nc.files_search("budget")` returns matches without bodies.
6. `nc.notes_create("Shopping", "milk\neggs")` lands in the NC Notes app and is visible on a separately-logged-in client within 5 s.
7. Revoking the app password in NC's UI flips the card to "auth failed" within one health-check cycle.

**Vault**
8. Existing `VAULT_PLAN.md §7` steps still pass.
9. `vault.create_login("Router", "admin", "<pw>", "http://192.168.178.1")` from the agent triggers the WhatsApp confirm loop; declining the confirm leaves the vault unchanged.

**Email**
10. Add a Proton account → Bridge container starts → `email.list_unread` returns the real inbox count.
11. Add a Gmail account with an app password → coexists with Proton, both selectable per call.
12. `email.draft` creates a draft visible in the user's Drafts folder; `email.send_direct` is gated on a setting and requires master-password re-entry.

**Self / mcp-homebrain**
13. `homebrain.backup_now` triggers `backup.sh`, dashboard task progress is visible, agent gets a completion notification when the JSON status file flips to `done`.
14. `homebrain.service_restart("nextcloud")` restarts the container and survives crash-loop protection.

**Cross-cutting**
15. Pull every `*.token` file → all are mode 0600, owned by `homebrain`, encrypted-at-rest where the design says so.
16. Hand-edit `openclaw.json mcp.servers[]` to add a bogus entry → on next dashboard load, the reconcile job logs a warning and does not silently overwrite (data preservation > tidiness).
17. `backup.sh` archive contains `~/.openclaw/*.token`, `~/.openclaw/.email/`, last 30 days of `mcp-*-audit.log`.

---

## 8. Security checklist (must hold before merge)

- [ ] All `*.token` files: mode 0600, owned by `homebrain`, never world-readable.
- [ ] `EMAIL_ENCRYPTION_KEY` derived from `MASTER_PASSWORD` via Argon2id; per-account credentials encrypted with it on disk.
- [ ] No `shell=True` in any MCP wrapper or provisioning script (CLAUDE.md invariant).
- [ ] Rate limit every `act` tool to ≤ 1 call/2s and ≤ 30 calls/hour, per chat-id, at the MCP layer.
- [ ] The WhatsApp consent token is single-use, 60s TTL, scoped to chat-id; replaying a captured token from a different chat fails closed.
- [ ] HA MCP allowlist denies: `homeassistant.restart`, `recorder.*`, `system_log.*`, `persistent_notification.create_with_html`, anything in the existing `config/openclaw.json` `denyCommands` list.
- [ ] Email MCP defaults to **drafts only** for sending. `send_direct` requires explicit Settings opt-in plus master-password re-entry.
- [ ] Audit logs: append-only by file mode, rotated weekly, included in backup with size cap.
- [ ] Pangolin tunnel does *not* expose any MCP port. Every MCP is stdio-only, lives in the OpenClaw process tree, never accepts a network socket.
- [ ] Login rate-limit middleware extended to `/api/integrations/*` endpoints.
- [ ] Reconciler refuses to overwrite hand-edited `openclaw.json` without an explicit "Reset MCP config" button click.

---

## 9. Phasing

| Phase | Scope | Exit criterion |
|---|---|---|
| **P1 — Skeleton** | `src/integrations.py` blueprint, `_integrations_card.html`, reconciler that writes `openclaw.json mcp.servers[]`, dummy "homebrain-self" MCP that returns version info. | Dashboard shows Connections page; OpenClaw lists `homebrain.*` tools. |
| **P2 — Home Assistant** | HA LLAT auto-bootstrap, `mcp-homeassistant.py`, allowlist, tests. | Agent toggles a real light over WhatsApp on the test rig. |
| **P3 — Nextcloud** | `occ`-driven app password, `mcp-nextcloud.py`, file/notes/calendar tools. | Agent creates a note from WhatsApp, verifies on a second NC client. |
| **P4 — Vault upgrade** | Auto-wire vault MCP into `openclaw.json` (drop the manual step), add `vault.create_login`, tighten audit. | "Save this to my vault" works end-to-end with WhatsApp confirm. |
| **P5 — Email** | `mcp-email.py`, account-add UI, optional Proton Bridge profile, drafts-by-default. | Proton + Gmail accounts both readable; drafts created from chat. |
| **P6 — Consent loop hardening** | `mcp_consent.py`, per-action TTL tokens, dashboard-confirm path for critical actions. | TESTING.md §6.9 (Vault confirm) and §6.12 (email send_direct) both green. |
| **P7 — Polish** | Audit log viewer, integration health monitoring on dashboard, "Reset MCP config" button, doc pass. | README.md and ROADMAP.md updated; merge to main. |

P1–P3 are MVP. P4–P5 ship in the same release if Bridge stabilises; otherwise P5 is a follow-up minor.

---

## 10. Open questions

1. **HA core MCP vs our shim.** Do we *require* HA ≥ 2025.x for the official `mcp_server`, or always ship our shim and let the official path be a "use this if you'd like" override? Recommendation: ship the shim as primary for predictability across HA versions; offer a one-click switch.
2. **Email sending without dashboard hop.** Power users will want `email.send_direct` without bouncing through drafts. Current plan: opt-in setting + master-password re-entry. Is that ergonomic enough? Alternative: per-recipient allowlist learned from the user's sent folder over the first 30 days.
3. **Calendar conflicts across NC + HA + Email.** Both NC (CalDAV) and most email providers (iCalendar invites) are calendars. Do we deduplicate at the MCP layer (`calendar.list` aggregates both) or expose them as separate tools and let the agent reason about it? Lean: aggregate, because the user does not care which silo holds which event.
4. **Per-integration model selection.** Some calls (email summarisation) want a smaller fast model; others (Vault unlock interpretation) want the main 35B. Worth wiring per-tool model overrides into `openclaw.json`'s agent config? Lean: not in v1 — measure first.
5. **Where does the consent log live?** Today: `/var/log/homebrain/mcp-*-audit.log`. Future: surface in the dashboard's existing log viewer with a "Audit" filter, so a non-technical user can review what the agent did this week. Cheap; do it in P7.

---

## 11. What this plan deliberately *does not* do

- **No federated identity / SSO.** Every integration uses its own scoped token. SSO is a v2 problem and would require running an OIDC provider next to Pangolin — disproportionate for a single-household appliance.
- **No agent-side guardrail prompt.** The MCP servers are the trust boundary. We do not rely on the model "knowing not to" send the user's password — we rely on `vault.search` *physically not returning passwords*. Defence at the right layer.
- **No multi-user separation in v1.** Family members share the OpenClaw instance and therefore see the same MCP surface. Per-WhatsApp-id ACLs are a v1.1 feature; v1 ships with a single owner.
- **No third-party LLM fallback.** All inference stays local on the box. Email integration is the only place outside data leaves the LAN, and only to the user's own provider.
- **No bespoke web UI per integration.** The Connections page is the entire UI surface. The actual data lives in NC's web UI / HA's UI / Vaultwarden's web vault / the user's email client. We do not re-implement those.

---

## 12. Sources

Home Assistant
- [HA Model Context Protocol Server (official)](https://www.home-assistant.io/integrations/mcp_server/)
- [HA Model Context Protocol (client)](https://www.home-assistant.io/integrations/mcp/)
- [homeassistant-ai/ha-mcp (community fallback)](https://github.com/homeassistant-ai/ha-mcp)

Nextcloud
- [cbcoutinho/nextcloud-mcp-server](https://github.com/cbcoutinho/nextcloud-mcp-server)
- [No-Smoke/nextcloud-mcp-comprehensive (broader surface)](https://github.com/No-Smoke/nextcloud-mcp-comprehensive)
- [Nextcloud AI Agent Integration: 2026 Guide](https://fast.io/resources/ai-agent-nextcloud-integration/)

Email / Protonmail
- [shenxn/protonmail-bridge-docker](https://github.com/shenxn/protonmail-bridge-docker)
- [VideoCurio/ProtonMailBridgeDocker](https://github.com/VideoCurio/ProtonMailBridgeDocker)
- [ai-zerolab/mcp-email-server](https://github.com/ai-zerolab/mcp-email-server)
- [n24q02m/better-email-mcp](https://github.com/n24q02m/better-email-mcp)

Vault (existing in repo)
- `VAULT_PLAN.md`, `scripts/mcp-vault.py`, `config/openclaw-mcp-vault.example.json`
