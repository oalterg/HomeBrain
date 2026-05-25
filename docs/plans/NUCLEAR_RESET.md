# NUCLEAR RESET — Formal Implementation Plan

**Document ID**: `plans/NUCLEAR_RESET.md`  
**Version**: 1.0  
**Date**: 2026-05-25  
**Status**: Design Complete — Awaiting Implementation Approval  
**Author**: Senior Engineering Review (opencode)  
**Reviewers**: (to be assigned)

---

## 1. Executive Summary

HomeBrain currently has no mechanism for a user to perform a complete factory reset from the dashboard without physical access or external tooling.

This plan defines a **"Nuclear Reset"** feature (also referred to internally as "Hard Reset" or "Factory Wipe") that:

- Completely erases all user data, runtime configuration, secrets, containers, volumes, and AI state.
- Leaves the device in a state **indistinguishable from a freshly imaged + provisioned unit**.
- Preserves only true factory data (the device sticker password and any baked-in Pangolin/Registrar credentials).
- Generates and hands over a **brand new master password** using the exact same credential claim flow as first boot.
- Ends with an automatic reboot so the device comes up clean.

The feature is intentionally **destructive and irreversible**. It is protected by multiple layers of confirmation and is rate-limited far more aggressively than any other privileged action.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- Deliver a first-class, dashboard-driven "Nuclear Reset" option.
- Guarantee that after reset the device requires the **factory password** (from the label) to begin a completely fresh setup.
- Produce a newly generated 16-character `MASTER_PASSWORD` that is shown exactly once (identical UX to initial provisioning).
- Make the operation safe against power loss, accidental triggering, and partial execution.
- Preserve the existing security invariants of the system (no `shell=True` abuse, atomic secret handling, HttpOnly cookies, etc.).

### 2.2 Non-Goals (Explicitly Out of Scope for v1)

- Remote wipe / kill switch triggered from the cloud registrar or Pangolin.
- Selective per-service resets (e.g., "only reset Nextcloud").
- Wiping or unmounting the external backup drive at `/mnt/backup`.
- Physical disk erasure / secure wipe of the OS drive (this is a software reset, not a forensic destroyer).
- Support for "reset and keep network settings" variants.

---

## 3. Scope: Data to Preserve vs. Destroy

### 3.1 Must Survive (Factory / Hardware Identity Only)

| Item | Path | Rationale |
|------|------|---------|
| Factory config | `/boot/firmware/factory_config.txt` (Pi) or `/opt/homebrain/factory_config.txt` (x86) | Contains `FACTORY_PASSWORD` + optional baked tunnel/registrar secrets. This is the only authentication secret that survives. |
| Application code | `/opt/homebrain/` (entire tree except runtime-generated files) | The manager, scripts, templates, etc. |
| GPU hardening | Kernel cmdline, `/etc/udev/rules.d/99-amdgpu-runpm.rules`, `/etc/modprobe.d/homebrain-amdgpu.conf` | These are platform-level reliability settings, not user data. |
| OS user | `homebrain` system user + `/home/homebrain/.ssh` (if present) | Required for the appliance model. |

### 3.2 Must Be Destroyed (User Data & Runtime State)

**Docker Layer**
- All named volumes via `docker compose down -v`:
  - `db_data`, `redis_data`, `nextcloud_html`, `ha_config`, `caddy_data`, `caddy_config`, `proton_bridge_data`

**Host Bind-Mount Data (Mandatory Wipe)**
- `${HOMEBRAIN_HOME}/nextcloud-data`
- `${HOMEBRAIN_HOME}/vault-data`
- `${HOMEBRAIN_HOME}/.openclaw` (entire tree, including workspace, tokens, MCP servers, integrations, vault.session, email_accounts.json, etc.)

**AI Layer (Default: Full Wipe)**
- `${HOMEBRAIN_HOME}/models/` — **wiped by default** (per resolved decision #1)
- AI model selection keys in `.env` (`AI_MODEL_*`)

**Configuration & State Markers (All Removed)**
- `${INSTALL_DIR}/.setup_complete`
- `${INSTALL_DIR}/.setup_started`
- `${INSTALL_DIR}/install_creds.json`
- `${INSTALL_DIR}/.install_creds_staging`
- `${INSTALL_DIR}/.first_boot_update_done`
- `${INSTALL_DIR}/.env` (deleted entirely; recreated from template on next setup)
- `${INSTALL_DIR}/docker-compose.override.yml`
- Backup cron: `/etc/cron.d/homebrain-backup`
- Any `VAULT_ADMIN_TOKEN`, nonces, derived keys, etc.

**Systemd / Runtime**
- llama-server and whisper-server systemd units (stop + disable best-effort)
- OpenClaw user-level units and runtime state for the `homebrain` user

**Explicitly NOT Touched**
- `/mnt/backup` and its fstab entry (resolved decision #3)
- Pre-compiled AI runtime binaries under `${HOMEBRAIN_HOME}/ai-runtime/` (llama-server, whisper-server) — **kept by default**; optional checkbox to delete (resolved decision #2)

---

## 4. Resolved Design Decisions

All open questions from the initial analysis have been closed:

1. **AI models** — Wiped **by default**. The primary checkbox "Delete downloaded AI models (saves disk space, requires re-download)" is checked by default and the wipe occurs unless the user explicitly unchecks it.

2. **AI runtime binaries distinction** — Yes, we offer the distinction. A secondary, collapsed "Advanced" section contains:
   - "Also delete AI runtime binaries (llama-server / whisper-server)" — **unchecked by default**.
   - Deleting these forces a full recompile/reinstall of llama.cpp + whisper.cpp on the next AI enable (slow and requires internet).

3. **Backup drive fstab** — Do **not** touch the `/mnt/backup` mount or fstab entry. The nuclear reset never unmounts or removes external storage references.

4. **Confirmation phrase** (senior engineer decision) — The user must type the exact string (case-sensitive, no extra spaces):
   ```
   DESTROY ALL DATA
   ```
   Rationale: Short enough to type, unambiguous in intent, difficult to trigger by muscle memory or typo, and directly states the consequence without requiring a full sentence.

5. **Automatic reboot** — Yes. A successful nuclear reset **always** ends with `reboot`. The script writes a final status marker before calling reboot so the UI can give the user a clean "rebooting" message.

6. **Last reset timestamp** — Not implemented in v1 (per request). Audit log entries in `/var/log/homebrain/` are considered sufficient for support and forensics.

---

## 5. Security & Safety Requirements

- **Authentication**: Only available after setup is complete. Requires a valid session + submission of the current `MANAGER_PASSWORD`.
- **Rate Limiting**: Maximum **1 attempt per 10 minutes** (global, not per-IP, because this is a privileged appliance action).
- **Confirmation**: Password + exact typed phrase (`DESTROY ALL DATA`) + optional AI checkboxes.
- **Session Invalidation**: On success, **all** active sessions are immediately terminated (existing `session.pop` + secret key rotation is acceptable but full cookie invalidation via a new secret key rotation is preferred).
- **Atomicity & Crash Safety**:
  - Use a lock file (`/var/run/homebrain-nuclear-reset.lock`).
  - Write progress to the standard task status file.
  - The script must be re-runnable after a power loss or `kill -9` without leaving the system in a half-wiped state (best-effort idempotency).
- **Logging**: Every nuclear reset writes a clear banner to `manager.log`:
  ```
  === NUCLEAR RESET INITIATED BY DASHBOARD ===
  User confirmed with phrase "DESTROY ALL DATA"
  Timestamp: ...
  ```
- **No shell injection paths**: The Flask endpoint must never pass unsanitized user input into any command.
- **Post-reset exposure**: After reset the only way back in is the factory password over the LAN (or existing tunnel if still configured in factory_config). No old master password or derived tokens can work.

---

## 6. Architecture & Component Responsibilities

### 6.1 New Files

| Path | Purpose | Owner |
|------|---------|-------|
| `scripts/nuclear_reset.sh` | The actual destructive logic. Must be idempotent and safe to re-run. | Bash |
| `plans/NUCLEAR_RESET.md` | This document (already created) | Docs |
| (future) `tests/e2e/test_nuclear_reset.py` or equivalent in TESTING.md | E2E checklist items | QA |

### 6.2 Modified Files

- `src/app.py` — Add the new API endpoint + any supporting helpers.
- `src/templates/dashboard.html` — Add the Danger Zone card + confirmation modal + progress UI.
- `TESTING.md` — Add a full "Nuclear Reset" verification section.
- (optional but recommended) `README.md` or a new "Recovery" section mentioning the feature.

### 6.3 Background Task Model (Reuse)

The feature **must** reuse the existing pattern:
- `STATUS_FILE = /tmp/homebrain_task_status.json`
- `POST /api/task_status` polling from the frontend
- `run_background_task(...)` helper (or a dedicated variant)
- The script writes structured JSON status updates at major phases.

---

## 7. Detailed Implementation Specification

### 7.1 Bash Script: `scripts/nuclear_reset.sh`

**Invocation**: Called by the manager as root with no arguments (all decisions come from the request body that the Python side already validated).

**High-Level Phases** (must be logged and reflected in status):

1. **Pre-flight & Locking**
   - Root check
   - Acquire exclusive lock
   - Verify `.setup_complete` exists (defensive)
   - Load common.sh + env

2. **Clean Shutdown**
   - Stop OpenClaw daemon (user-level)
   - Stop llama-server + whisper-server
   - `docker compose ... down` (graceful)

3. **Volume & Data Destruction**
   - `docker compose down -v`
   - `rm -rf` the three mandatory host trees
   - Conditional: wipe `${HOMEBRAIN_HOME}/models` (default yes)
   - Conditional: wipe `${HOMEBRAIN_HOME}/ai-runtime` (default no)

4. **State & Config Erasure**
   - Delete all markers and `.env`
   - Remove override files, backup cron, etc.
   - Best-effort: `systemctl --user -M homebrain@ reset-failed` for OpenClaw units

5. **New Credential Generation (Critical)**
   - Generate fresh 16-character alphanumeric `MASTER_PASSWORD` (same alphabet and method as `start_setup`)
   - Write `install_creds.json` in the **exact** shape expected by the existing handover UI:
     ```json
     {
       "username": "admin",
       "password": "<new>",
       "domain": "...",
       "generated_at": <unix>
     }
     ```
   - Stage via `.install_creds_staging` then atomic move (mirror deploy.sh pattern)

6. **Final Status & Reboot**
   - Write final success status: `{"status":"success","message":"Nuclear reset complete. Rebooting..."}`
   - Log the banner
   - `sync; sleep 2; reboot`

The script must never remove the lock file on the reboot path (the reboot will clear `/tmp` anyway).

### 7.2 Flask API: `POST /api/system/nuclear-reset`

**Request** (JSON):
```json
{
  "current_password": "the-current-master-password",
  "confirmation_phrase": "DESTROY ALL DATA",
  "wipe_ai_models": true,
  "wipe_ai_runtime": false
}
```

**Responses**:
- 200: `{"status":"started"}`
- 400: bad confirmation phrase or missing password
- 401: unauthenticated or wrong current password
- 409: another task is running
- 429: rate limited

The endpoint:
- Validates the current password against `MANAGER_PASSWORD` from `.env`
- Enforces the exact phrase match
- Checks rate limit (use a dedicated limiter instance or a simple timestamp file under `/tmp`)
- Spawns the background thread
- Returns immediately

### 7.3 Session & Auth Handling

On successful completion (detected by the frontend when status becomes success):
- The Python side (or a one-shot cleanup on next request) must invalidate the session.
- Recommended: rotate `app.secret_key` by deleting the `.secret_key` file (forces all sessions invalid on restart). This is safe because the device is about to reboot.

### 7.4 Frontend UX (dashboard.html)

**Location**: New card in the **Settings** tab titled "Danger Zone" (red border, at the very bottom).

**Button**: Large red "Nuclear Reset / Factory Reset Device"

**Modal Content** (exact required elements):
- Headline: "This action cannot be undone."
- Bullet list of what will be destroyed (Nextcloud files, Home Assistant configuration, Vaultwarden vault, OpenClaw memory & chats, all passwords, AI models, etc.).
- Explicit statement: "Your external backup drive at /mnt/backup will **not** be touched."
- Current master password input (type=password)
- Confirmation phrase input (type=text, placeholder: Type DESTROY ALL DATA exactly)
- Two checkboxes (default states per decisions):
  - [x] Delete downloaded AI models (~10–30 GB, will require re-download)
  - [ ] Also delete AI runtime binaries (forces slow reinstall later)
- Final warning in red: "After reset the device will reboot. You will need the factory password on the device label to log in again."
- Submit button: "I understand — Perform Nuclear Reset" (disabled until all fields are valid)

**Progress State**:
- Full-screen or prominent blocking overlay once the API call succeeds.
- Live log tail (reuse existing log viewer component) filtered to the nuclear reset log lines if possible.
- When status reports success + "Rebooting...", show countdown + "The device is now rebooting. This page will stop responding. Wait 60–90 seconds then reconnect."

---

## 8. Post-Reset State Machine

1. User triggers reset from authenticated dashboard.
2. Script runs → all data gone → `install_creds.json` written with new password.
3. Reboot.
4. On boot, manager starts.
5. First HTTP request:
   - `is_setup_complete()` → false
   - No `install_creds.json` yet visible? Wait — it is present.
   - Welcome + login gate uses **factory password**.
6. User logs in with factory password → sees the "Setup complete — here are your new credentials" screen (exact same component as initial provisioning).
7. User claims credentials → normal post-setup dashboard.
8. New `MASTER_PASSWORD` is now active everywhere.

This flow is **identical** to a brand-new device. No special "post-reset" code paths are required in the setup wizard.

---

## 9. Testing Requirements (Mandatory Additions to TESTING.md)

Add a new top-level section:

### Nuclear Reset (Destructive — Run Last on Test Hardware)

**Prerequisites**
- Freshly provisioned device with some user data (files in Nextcloud, at least one HA entity, one Vault login, OpenClaw chat history if GPU, external backup drive mounted).

**Test Matrix** (both Local and Remote modes, GPU and non-GPU where applicable):

- [ ] Trigger nuclear reset with wrong current password → rejected
- [ ] Trigger with correct password but wrong/misspelled phrase → rejected
- [ ] Successful trigger with default options (models wiped, runtime kept)
- [ ] Verify post-reboot:
  - Login only possible with factory password
  - New master password shown exactly once
  - No old Nextcloud files, no old HA config, no old Vault data, no old .openclaw workspace
  - AI models directory is gone (if GPU)
  - External backup drive still mounted and readable
- [ ] Repeat with "also wipe AI runtime" checked → verify ai-runtime/ is empty after reset
- [ ] Power-loss simulation: kill the nuclear script mid-execution, reboot, re-trigger → script must either complete cleanly or leave the device in a state where re-triggering finishes the job
- [ ] Rate-limit test: second attempt within 10 minutes is rejected with clear message
- [ ] Verify that old browser sessions are fully invalidated after reset + reboot

All tests must be executed on real hardware before the PR is eligible for merge.

---

## 10. Risks, Mitigations & Rollback

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|----------|
| Accidental trigger by owner | Medium | Catastrophic (data loss) | Multi-layer confirmation + rate limit + explicit phrase |
| Power loss during wipe | Low | High (inconsistent state) | Idempotent script design + re-runnability |
| User forgets factory password after reset | Medium | High (device bricked for them) | Already documented as irrecoverable during initial provisioning; reinforce in modal |
| Old tunnel tokens still work | Low | High (unauthorized access) | Complete volume + data destruction + reboot + new master password derivation |
| No practical rollback | Certain | — | This is a destructive reset by design. Only recovery is from a prior backup archive via the normal restore flow (which will be available if the user had an external drive). |

There is **no software rollback** for a nuclear reset. The only recovery path is restoring from a backup taken before the reset.

---

## 11. Implementation Phases & Detailed Checklist

**Phase 0 — Planning (Complete)**
- [x] This document created in `plans/`
- [ ] Team / maintainer sign-off on all design decisions and confirmation phrase

**Phase 1 — Core Script & Backend (No UI yet)**
- [ ] Create `scripts/nuclear_reset.sh` with all phases, logging, locking, and credential generation
- [ ] Add `POST /api/system/nuclear-reset` endpoint with full validation + rate limiting
- [ ] Wire the background task status
- [ ] Manual smoke test via curl from the device (as root)

**Phase 2 — Frontend & UX Polish**
- [ ] Add Danger Zone card + modal in dashboard.html
- [ ] Implement live progress + reboot UX
- [ ] Add client-side validation for the phrase and password

**Phase 3 — Hardening & Documentation**
- [ ] Update `TESTING.md` with the full nuclear reset test matrix
- [ ] Add warning text in README.md (optional but recommended)
- [ ] Ensure `nuclear_reset.sh` is chmod +x during provision/deploy
- [ ] Security review of the new endpoint (rate limiter placement, no secret leakage in logs)

**Phase 4 — E2E Validation on Real Hardware**
- Must pass the entire matrix in Section 9 on at least one x86+GPU and one non-GPU (RPi or equivalent) device before merge.

**Phase 5 — Release**
- Conventional commit: `feat: add nuclear reset (factory wipe) with new master password handover`
- Update ROADMAP.md

---

## 12. Appendices

### A. Exact Confirmation Phrase (Final)

User must type (exactly, including case):
```
DESTROY ALL DATA
```

### B. Example Final Status Written Before Reboot

```json
{
  "status": "success",
  "message": "Nuclear reset completed successfully. Rebooting now...",
  "log_type": "setup"
}
```

### C. Related Existing Code Paths (for reference during implementation)

- Credential generation & staging: `src/app.py:704` (start_setup) and `scripts/deploy.sh:152`
- Status file helpers: `src/app.py:335` (write_status) and `349` (read_status)
- Factory password reading: `src/app.py:305`
- Login password selection logic: `src/app.py:432`
- Existing destructive operation (restore): `scripts/restore.sh`

---

**End of Plan**

This document is the single source of truth for the Nuclear Reset feature. No implementation work may begin until explicit approval is given.
