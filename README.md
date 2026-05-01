# HomeBrain

A private cloud + AI assistant in a box. One machine, one provisioning command, no SaaS dependencies.

HomeBrain bundles **Nextcloud** (file sync), **Home Assistant** (smart home), and — when a GPU is present — **OpenClaw** (local AI assistant on WhatsApp, backed by Qwen3.6 via llama.cpp) into a single stack. Reach it from anywhere over a self-hosted **Pangolin** tunnel, or keep it on the LAN with no tunnel at all.

---

## Editions

The same codebase ships in two flavors, picked automatically by the architecture detector at provision time — no flag to set:

| Edition | Target | What runs | Best for |
|---|---|---|---|
| **HomeBrain** (full) | x86_64 + AMD GPU | Nextcloud · Home Assistant · llama.cpp · whisper.cpp · OpenClaw | Households that want a private LLM and voice control alongside file sync and home automation |
| **HomeCloud** (AI-disabled) | aarch64 (Raspberry Pi 4 / 5) or any x86_64 box without a GPU | Nextcloud · Home Assistant · (optional) Pangolin tunnel | A quiet, low-power family cloud and smart-home hub with optional internet access through your own tunnel |

The Pangolin remote-access tunnel is available in both editions; the AI stack only ships on x86_64 + AMD GPU.

---

## Hardware Requirements

### HomeBrain (full)

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Ubuntu 24.04 LTS (x86_64) | Ubuntu 24.04 LTS or 25.10 |
| RAM | 16 GB | 32 GB |
| Storage | 64 GB SSD | 512 GB NVMe (model files alone are ~25 GB) |
| GPU | AMD Radeon RX 6000-series, 8 GB VRAM | AMD Radeon RX 9060 XT or newer, **16 GB VRAM** |
| Network | Ethernet | Ethernet (Gigabit) |

Inference runs on Vulkan via Mesa RADV — no ROCm install required. See [BENCHMARKS.md](BENCHMARKS.md) for measured throughput across quantizations on a Radeon RX 9060 XT.

### HomeCloud (AI-disabled)

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Board | Raspberry Pi 4 (4 GB) | Raspberry Pi 5 (8 GB) — or any x86_64 mini-PC |
| OS | Raspberry Pi OS 64-bit (Bookworm) or Ubuntu Server 24.04 (arm64/x86_64) | same |
| RAM | 4 GB | 8 GB |
| Storage | 32 GB microSD/SSD | 256 GB SSD/NVMe over USB 3 or PCIe |
| Network | Ethernet | Ethernet (Gigabit) |

Architecture is auto-detected: any non-x86_64 host is treated as no-GPU and the AI stack is skipped. The dashboard, backups, and Pangolin tunnel all behave identically to the full edition.

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

1. **64-bit OS, freshly installed** — Ubuntu 24.04+ on x86_64 (HomeBrain), or Raspberry Pi OS 64-bit / Ubuntu Server arm64 on a Pi 4/5 (HomeCloud). SSH accessible either way.
2. **`homebrain` OS user** — created during provisioning if missing
3. **For Remote Access mode**: Pangolin tunnel credentials (`NEWT_ID`, `NEWT_SECRET`, tunnel domain, Pangolin endpoint)
4. **For AI features (HomeBrain only)**: AMD GPU with ≥ 16 GB VRAM. Inference uses Vulkan via Mesa RADV — no ROCm setup needed.
5. **BIOS / firmware**: *Restore on AC Power Loss* → **Power On** (or the equivalent Pi PSU watchdog setting) so the server auto-starts after a power outage

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
HomeBrain / HomeCloud
├── Nextcloud          (Docker)            — file sync, CalDAV, CardDAV
├── Home Assistant     (Docker)            — smart home automation
├── MariaDB            (Docker)            — Nextcloud database
├── Pangolin Newt      (Docker, optional)  — tunnel client
├── llama-server       (systemd, x86 GPU)  — local LLM inference (HomeBrain only)
├── whisper-server     (systemd, x86 GPU)  — speech-to-text     (HomeBrain only)
└── OpenClaw           (systemd, x86 GPU)  — AI assistant + WhatsApp (HomeBrain only)
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
