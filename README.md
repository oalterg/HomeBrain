# HomeBrain

A private cloud + AI assistant in a box. One Ubuntu machine, one provisioning command, no SaaS dependencies.

HomeBrain bundles **Nextcloud** (file sync), **Home Assistant** (smart home), and **OpenClaw** (local AI assistant on WhatsApp, backed by Qwen3.6 via llama.cpp) into a single stack. Reach it from anywhere over a self-hosted **Pangolin** tunnel, or keep it on the LAN with no tunnel at all.

---

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Ubuntu 24.04 LTS (x86_64) | Ubuntu 24.04 LTS or 25.10 |
| RAM | 16 GB | 32 GB |
| Storage | 64 GB SSD | 512 GB NVMe (model files alone are ~25 GB) |
| GPU | — (AI features disabled) | AMD Radeon RX 6000-series or newer, **16 GB VRAM** |
| Network | Ethernet | Ethernet (Gigabit) |

**GPU note:** The AI stack (llama.cpp inference + OpenClaw assistant) auto-enables when a compatible AMD GPU is present. Inference runs on Vulkan via Mesa RADV — no ROCm install required. Without a GPU, HomeBrain runs as a privacy-first Nextcloud + Home Assistant server with the AI features disabled. See [BENCHMARKS.md](BENCHMARKS.md) for measured throughput across quantizations on a Radeon RX 9060 XT.

---

## Deployment Modes

HomeBrain supports two deployment modes, chosen during setup:

### 🌐 Remote Access
Accessible from anywhere via a **Pangolin** self-hosted tunnel (no static IP, no port forwarding). Requires Pangolin tunnel credentials.

- Services available at `cloud.yourdomain.com`, `nc.cloud.yourdomain.com`, `ha.cloud.yourdomain.com`
- End-to-end encrypted tunnel
- No cloud intermediary — traffic routes through your own Pangolin instance

### 🏠 Local Network Only
No tunnel. Services available on the local network at `homebrain.local` (mDNS) or by LAN IP. No registration or external credentials required.

- `http://homebrain.local`      → HomeBrain Dashboard (port 80)
- `http://homebrain.local:8080` → Nextcloud
- `http://homebrain.local:8123` → Home Assistant

---

## Features

- **Privacy-first**: All data stays on your hardware. No telemetry, no cloud sync, no vendor lock-in.
- **One-command provisioning**: A browser-based setup wizard handles passwords, tunnel credentials, model selection, and deployment mode.
- **Local AI assistant**: [OpenClaw](https://openclaw.ai) runs a WhatsApp-connected agent backed by local llama.cpp inference. Default model is Qwen3.6-35B-A3B (MoE); lighter quantizations selectable from the dashboard.
- **Tuned inference**: Vulkan-RADV configuration tuned for AMD RDNA3/RDNA4 — selective MoE expert offload, q8_0 KV cache, flash attention, `RADV_PERFTEST=rm_kq=1`. ~29 t/s generation, ~750 t/s prompt processing on a 16 GB card at 131K context. See [BENCHMARKS.md](BENCHMARKS.md).
- **Automated backups**: Scheduled snapshots with configurable retention. Covers Nextcloud data, Home Assistant config, and OpenClaw agent workspace/memory.
- **Reliability hardening**: AMD GPU runtime power management disabled (VRAM stays resident between requests), systemd crash-loop protection, sleep inhibitor.
- **Dashboard**: Real-time GPU utilisation, VRAM, and temperature; log viewer for all services; in-place updates via pinned `versions.json`.

---

## Prerequisites

1. **Ubuntu 24.04+ x86_64** — freshly installed, SSH accessible
2. **`homebrain` OS user** — created during provisioning
3. **For Remote Access mode**: Pangolin tunnel credentials (`NEWT_ID`, `NEWT_SECRET`, tunnel domain, Pangolin endpoint)
4. **For AI features**: AMD GPU with ≥ 16 GB VRAM. Inference uses Vulkan via Mesa RADV — no ROCm setup needed.
5. **BIOS setting**: *Restore on AC Power Loss* → **Power On** so the server auto-starts after a power outage

---

## Installation

### 1. Bootstrap

```bash
curl -fsSL https://raw.githubusercontent.com/oalterg/homebrain/main/install | sudo bash
```

### 2. Provision

The recommended path is the **browser setup wizard** at `http://<server-ip>` — it walks through deployment mode, credentials, and model selection. For headless deployments, run `provision.sh` directly:

**Remote Access mode** (Pangolin tunnel):
```bash
sudo /opt/homebrain/scripts/provision.sh \
  "<NEWT_ID>" "<NEWT_SECRET>" "<TUNNEL_DOMAIN>" "<PANGOLIN_ENDPOINT>" "<FACTORY_PASSWORD>"
```

**Local Network only** (no tunnel):
```bash
sudo /opt/homebrain/scripts/provision.sh "<FACTORY_PASSWORD>"
```

Omit `<FACTORY_PASSWORD>` to have one generated and printed at the end of provisioning.

### 3. Reboot

```bash
sudo reboot
```

After reboot, access the dashboard at your tunnel domain (Remote) or `http://homebrain.local` (Local).

---

## Configuration

All runtime configuration lives in `/opt/homebrain/.env`. Key variables:

| Variable | Description |
|----------|-------------|
| `MASTER_PASSWORD` | Master password — authenticates all services |
| `DEPLOYMENT_MODE` | `remote` or `local` — set by setup wizard |
| `HAS_GPU` | `true`/`false` — auto-detected at provision time |
| `PANGOLIN_DOMAIN` | Tunnel domain (Remote mode only) |
| `BACKUP_OPENCLAW_WORKSPACE` | Include OpenClaw agent memory in backups (default: `true`) |

---

## Architecture

```
HomeBrain
├── Nextcloud          (Docker)            — file sync, CalDAV, CardDAV
├── Home Assistant     (Docker)            — smart home automation
├── MariaDB            (Docker)            — Nextcloud database
├── Pangolin Newt      (Docker, optional)  — tunnel client
├── llama-server       (systemd, GPU)      — local LLM inference (llama.cpp + Vulkan)
├── whisper-server     (systemd, GPU)      — speech-to-text (whisper.cpp + Vulkan)
└── OpenClaw           (systemd, GPU)      — AI assistant + WhatsApp integration
```

Updates are pinned in [`config/versions.json`](config/versions.json) and applied via the dashboard's "Update" button — bumping the pinned `llama_cpp.tag` automatically re-downloads and restarts the inference binary on the next update click. See [BENCHMARKS.md](BENCHMARKS.md) for the inference tuning rationale and [TESTING.md](TESTING.md) for the E2E verification checklist used before every merge to `main`.

---

## Documentation

- [BENCHMARKS.md](BENCHMARKS.md) — measured throughput across quantizations, hardware tuning notes
- [ROADMAP.md](ROADMAP.md) — planned features and shipped releases
- [TESTING.md](TESTING.md) — E2E verification checklist
- [CLAUDE.md](CLAUDE.md) — repo conventions for AI-assisted contribution

---

## License

BSD-3-Clause. See [LICENSE](LICENSE) for details.
