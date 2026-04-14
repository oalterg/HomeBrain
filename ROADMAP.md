# HomeBrain Roadmap

Track planned features and shipped releases. For questions or contributions, open a GitHub issue.

---

## Shipped

### openclaw-integration branch (2026)

- ✅ **OpenClaw AI assistant integration** — WhatsApp-connected local AI via llamacpp
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

---

## Planned

### Near-term

- **llamacpp fine-tuning** — Qwen3.5 35B A3B Q6K, Q8 KV cache, 130K context, MoE CPU offloading for 16 GB VRAM
- **#2 · Dependency version management** — Pin OpenClaw and llama.cpp to verified, tested releases. Define a freeze/upgrade workflow so the stack can be reproduced and deliberately updated with confidence.

### Medium-term

- **MCP servers** — Protonmail and Nextcloud MCP integrations for OpenClaw agent

---

## How to contribute

Open a GitHub issue to report a bug or propose a feature. PRs welcome against the `main` branch.
