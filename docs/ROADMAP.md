# HomeBrain Roadmap

Track planned features and shipped releases. For questions or contributions, open a GitHub issue.

---

## Shipped

### openclaw-integration branch (2026)

- ✅ **OpenClaw AI assistant integration** — Telegram-connected local AI via llamacpp
- ✅ **GPU-gated AI stack** — AI features auto-enabled on GPU detection; RPi/no-GPU paths removed
- ✅ **Stable OpenClaw gateway token** — derived from MASTER_PASSWORD, persistent across restarts
- ✅ **Pre-authenticated dashboard link** — OpenClaw dashboard opens directly from HomeBrain (no token re-entry)
- ✅ **OS user migration** — single `homebrain` user; all `/home/admin` paths resolved
- ✅ **Backup drive filter** — NVMe partitions and system drives excluded from backup candidates
- ✅ **Localhost-only deployment** — full provisioning + Nextcloud config without Pangolin tunnel
- ✅ **Setup wizard deployment mode selector** — GUI choice between Local Network and Remote Access
- ✅ **Dashboard GPU stats** — real-time GPU utilisation, temperature, and VRAM usage (AMD sysfs, no rocm-smi)
- ✅ **Extended log viewer** — llamacpp and OpenClaw logs in dashboard (journalctl)
- ✅ **OpenClaw backup scope** — agent workspace and config included in automated backups (opt-out)
- ✅ **Always-on hardening** — AMD GPU runtime PM disabled (VRAM stays loaded), sleep inhibitor service, systemd crash-loop protection
- ✅ **Dependency version pinning** — OpenClaw and llama.cpp locked to verified, tested releases; freeze/upgrade workflow defined for reproducible stack
- ✅ **llamacpp fine-tuning** — Qwen3.6 35B A3B Q6K, benchmarked MoE CPU offloading for 16 GB VRAM
- ✅ **Consolidated project directories** — runtime data under `/home/homebrain/{ai-runtime,models,nextcloud-data,.openclaw}`, app code stays in `/opt/homebrain/`. Idempotent migration in `utilities.sh` covers legacy `/home/admin` and earlier flat layouts.

---

### Since v2026.06.12

- ✅ **HomeBrain Vault** *(v2026.06.12)* — Self-hosted Vaultwarden password manager: Bitwarden-compatible clients, MariaDB-backed, dashboard-managed bootstrap, Pangolin-tunnelled, backup-integrated. Document attachments via Vaultwarden + Nextcloud encrypted folder. See [`VAULT_PLAN.md`](plans/VAULT_PLAN.md).
- ✅ **OpenClaw integrations** *(v2026.06.12)* — Unified MCP-based access to Home Assistant, Nextcloud, Vault, and Email for the OpenClaw agent. Single Connections page in the dashboard, capability-tiered tools (Read/Act/Reveal), chat-native consent loop. All five servers verified live 2026-05-05. See [`INTEGRATIONS_PLAN.md`](plans/INTEGRATIONS_PLAN.md).
- ✅ **Master-password recovery phrase** *(v2026.06.12; x86 E2E 2026-06-15, RPi E2E 2026-08-01)* — A memorable word passphrase as the generated master password (B1), plus an independent word-based recovery code (B2) that resets the master password across the whole stack if it's forgotten. Stored only as a scrypt hash; shown once. LAN-only by default. Settings → **Master Password** drives the same rotation deliberately, and both secret reveals offer a `.txt` download. See [`RECOVERY_PHRASE.md`](plans/RECOVERY_PHRASE.md).
- ✅ **Off-site backup** *(v2026.07.24 → v2026.07.28)* — rclone mirror to sftp/webdav/s3, age-based retention, restore straight from the off-site copy.
- ✅ **Hardware agnosticism** *(v2026.07.25)* — One platform record (`detect_platform` in `common.sh`) replaces the `uname -m` guesswork that four separate decisions each made for themselves: llama.cpp binary selection, model flag profiles, GPU telemetry, and host hardening. Fixes an NVIDIA double-restart bug and the provision-installs-`latest` / update-installs-the-pin split along the way. See [`HARDWARE_AGNOSTIC.md`](plans/HARDWARE_AGNOSTIC.md).

---

## Planned

- **ARGB AI feedback Lighting** — CPU fan ARGB control on GPU/AI activity.

The next two tiers of work are planned in detail:

- [`TIER1_PROVE_IT.md`](plans/TIER1_PROVE_IT.md) — make the box prove the claims it makes, and
  fail loudly when it cannot. Bare-metal restore in the wizard, a system self-test, email as a
  second notification channel, a dead-man's switch.
- [`TIER2_AND_PHASE0.md`](plans/TIER2_AND_PHASE0.md) — limits real deployments have already hit.
  Moving Nextcloud data to a second drive, phone photo backup as a first-class feature.
- [`PHOTOS_HARDENING.md`](plans/PHOTOS_HARDENING.md) — follow-ups to the shipped photo path. A phone
  never holding an admin credential is done; storage quotas and HEIC/video thumbnails are recorded
  with evidence and still open.

---

## How to contribute

Open a GitHub issue to report a bug or propose a feature. PRs welcome against the `main` branch.
