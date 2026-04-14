# HomeBrain

Self-hosted private cloud and AI assistant server. Runs on a standard x86_64 Ubuntu machine with an AMD GPU.

HomeBrain bundles **Nextcloud** (file sync and storage), **Home Assistant** (smart home), and **OpenClaw** (local AI assistant via WhatsApp) into a single provisioned stack — remote-accessible over a Pangolin tunnel, or local-network-only with no tunnel required.

---

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Ubuntu 22.04 LTS (x86_64) | Ubuntu 24.04 LTS |
| RAM | 8 GB | 16 GB |
| Storage | 64 GB SSD | 256 GB NVMe |
| GPU | — (AI features disabled) | AMD RX 9060 or newer (≥ 8 GB VRAM) |
| Network | Ethernet | Ethernet (Gigabit) |

**GPU note:** The AI stack (llamacpp inference + OpenClaw AI assistant) is automatically enabled when a compatible AMD GPU is detected. Without a GPU, HomeBrain runs as a privacy-first Nextcloud + Home Assistant server.

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

- `http://homebrain.local:8080` → Nextcloud
- `http://homebrain.local:8123` → Home Assistant
- `http://homebrain.local:4000` → HomeBrain Dashboard

---

## Features

- **Privacy-first**: All data stays on your hardware. No telemetry, no cloud sync, no vendor lock-in.
- **One-command provisioning**: A setup wizard handles all configuration — passwords, tunnel credentials, deployment mode.
- **Local AI assistant**: [OpenClaw](https://openclaw.ai) runs a WhatsApp-connected AI agent backed by local llamacpp inference (GPU required). Powered by Qwen3.5 models.
- **Automated backups**: Scheduled snapshots with configurable retention. Backs up Nextcloud data, Home Assistant config, and OpenClaw agent workspace/memory.
- **Always-on hardening**: AMD GPU runtime power management disabled (VRAM stays loaded between requests), systemd crash-loop protection, sleep inhibitor service.
- **Dashboard**: Real-time system stats including GPU utilisation, VRAM, and temperature. Log viewer for all services including llamacpp and OpenClaw.

---

## Prerequisites

1. **Ubuntu 22.04+ x86_64** — freshly installed, SSH accessible
2. **`homebrain` OS user** — created during provisioning
3. **For Remote Access mode**: Pangolin tunnel credentials (`NEWT_ID`, `NEWT_SECRET`, tunnel domain, Pangolin endpoint)
4. **For AI features**: AMD GPU with ≥ 8 GB VRAM; ROCm-compatible (RX 5000 series or newer)
5. **BIOS setting**: Set *Restore on AC Power Loss* → **Power On** so the server auto-starts after a power outage

---

## Installation

### 1. Bootstrap

```bash
curl -fsSL https://raw.githubusercontent.com/oalterg/homebrain/main/install | sudo bash
```

### 2. Provision

**Remote Access mode** (Pangolin tunnel):
```bash
sudo /opt/homebrain/scripts/provision.sh \
  "<NEWT_ID>" "<NEWT_SECRET>" "<TUNNEL_DOMAIN>" "<PANGOLIN_ENDPOINT>" "<FACTORY_PASSWORD>"
```

**Local Network only** (no tunnel):
```bash
sudo /opt/homebrain/scripts/provision.sh "<FACTORY_PASSWORD>"
```

Alternatively, open the **setup wizard** in your browser at `http://<server-ip>:4000` to configure everything via the GUI.

### 3. Reboot

```bash
sudo reboot
```

After reboot, access the dashboard at your tunnel domain (Remote) or `http://homebrain.local:4000` (Local).

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
├── Nextcloud          (Docker) — file sync, CalDAV, CardDAV
├── Home Assistant     (Docker) — smart home automation
├── MariaDB            (Docker) — Nextcloud database
├── Pangolin Newt      (Docker, optional) — tunnel client
├── llamacpp server    (systemd) — local LLM inference (GPU required)
├── whisper-server     (systemd) — speech-to-text (GPU required)
└── OpenClaw           (systemd) — AI assistant + WhatsApp integration (GPU required)
```

---

## License

BSD-3-Clause. See [LICENSE](LICENSE) for details.
