# HomeBrain

**Your private cloud, smart home, and personal AI agent — in one box.**

No subscriptions. No cloud accounts. No data leaves your network.

HomeBrain provisions a complete self-hosted stack on a single machine: **Nextcloud** for files, calendars, and contacts, **Home Assistant** for smart-home automation, **Vaultwarden** for passwords, and — on GPU-equipped hardware — **OpenClaw**, a personal AI agent on WhatsApp or Telegram, powered by local llama.cpp inference. The agent has MCP integrations into every service in the stack: it reads your calendar, controls your lights, fetches your files, and drafts emails — all on-device, all private.

One command installs it. A browser wizard configures it. You own the whole thing.

<p align="center">
  <img src="res/screenshot.png" alt="HomeBrain Dashboard" width="820">
</p>

---

## Why HomeBrain

- **Truly private** — files, passwords, conversations, and home automation all run on hardware you control. Zero telemetry, zero cloud dependency.
- **An AI agent, not just a chatbot** — OpenClaw is a personal agent on WhatsApp or Telegram, backed by a local LLM (Qwen 3.6 35B MoE). It doesn't just answer questions — it acts: reading your calendar, toggling your lights, pulling files from Nextcloud, looking up passwords, sending emails. Every token stays on your hardware.
- **Your messenger becomes the interface** — the agent doubles as a de facto tunnel into your home stack. Ask it for a file and it sends it. Ask it to turn off the lights and it does. No VPN, no port forwarding, no app to install — just message it. For most day-to-day use, the Pangolin tunnel is optional.
- **One-command setup** — bootstrap, configure through a browser wizard, reboot. That's it.
- **Encrypted remote access** — for full web UI access to Nextcloud, Home Assistant, and Vault from outside your network, Pangolin tunnels everything end-to-end through your own infrastructure. No static IP, no middleman.
- **Full backup & restore** — scheduled snapshots with configurable retention, covering Nextcloud files, Home Assistant config, Vault, and AI agent memory. Restore from the dashboard in one click — or from the CLI on a fresh machine. External drive detection built in.

---

## What's Inside

| Service | What it does |
|---------|-------------|
| **Nextcloud** | File sync, CalDAV, CardDAV — your personal cloud |
| **Home Assistant** | Smart-home control and automation |
| **Vaultwarden** | Bitwarden-compatible password manager |
| **OpenClaw** | Personal AI agent on WhatsApp / Telegram with MCP access to every service above |
| **llama-server** | Local LLM inference on Vulkan (no ROCm required) |
| **Whisper** | Speech-to-text for voice messages |
| **Pangolin** | Self-hosted encrypted tunnel for remote access |

---

## Editions

The same codebase, two runtime profiles — detected automatically:

| | **HomeBrain** | **HomeCloud** |
|---|---|---|
| Target | x86_64 + AMD GPU | Raspberry Pi 4/5 or any x86 box |
| AI stack | Full (LLM + whisper + OpenClaw) | — |
| Cloud + smart home | Nextcloud · Home Assistant · Vault | Nextcloud · Home Assistant · Vault |
| Tunnel | Optional — agent on messenger covers most remote use | Optional |

If there's a GPU, you get a personal AI agent. If there isn't, you still get a powerful private cloud and smart-home hub.

---

## Reference Hardware

**HomeBrain** — our daily-driver build:

| Component | Spec |
|-----------|------|
| CPU | AMD Ryzen 5 / Intel i5 or better |
| RAM | 32 GB |
| Storage | 512 GB NVMe |
| GPU | AMD Radeon RX 9060 XT (16 GB VRAM) |
| OS | Ubuntu 24.04 LTS |

Inference runs on Vulkan via Mesa RADV — no ROCm install required. ~29 tok/s generation, ~750 tok/s prompt processing at 131K context. See [BENCHMARKS.md](docs/BENCHMARKS.md).

**HomeCloud** — Raspberry Pi 5 (8 GB) with an SSD, or any lightweight x86 mini-PC.

---

## Quick Start

```bash
# 1. Bootstrap (Ubuntu 24.04+ / RPi OS 64-bit)
curl -fsSL https://raw.githubusercontent.com/oalterg/HomeBrain/main/install | sudo bash

# 2. Open the setup wizard in your browser
#    http://<server-ip>

# 3. Reboot
sudo reboot
```

The wizard walks you through deployment mode (LAN or tunnel), passwords, and model selection. For headless installs, `provision.sh` accepts everything as arguments — see [Installation docs](#headless-provisioning) below.

### Headless Provisioning

**Remote access** (Pangolin tunnel):
```bash
sudo /opt/homebrain/scripts/provision.sh \
  "<NEWT_ID>" "<NEWT_SECRET>" "<TUNNEL_DOMAIN>" "<PANGOLIN_ENDPOINT>" "<PASSWORD>"
```

**Local network only:**
```bash
sudo /opt/homebrain/scripts/provision.sh "<PASSWORD>"
```

Omit the password to have one generated automatically.

---

## Architecture

```
HomeBrain
├── Nextcloud          Docker         Files, calendars, contacts
├── Home Assistant     Docker         Smart-home automation
├── Vaultwarden        Docker         Password manager
├── MariaDB            Docker         Nextcloud + Vault database
├── Pangolin Newt      Docker         Encrypted tunnel (optional)
├── llama-server       systemd        Local LLM inference (GPU only)
├── whisper-server     systemd        Speech-to-text (GPU only)
└── OpenClaw           systemd        AI agent · WhatsApp / Telegram (GPU only)
```

Updates are pinned in [`config/versions.json`](config/versions.json) and applied from the dashboard with a single click.

---

## Documentation

| Doc | What's in it |
|-----|-------------|
| [BENCHMARKS.md](docs/BENCHMARKS.md) | Inference throughput, quantization comparisons, tuning notes |
| [ROADMAP.md](docs/ROADMAP.md) | Shipped features and what's next |
| [TESTING.md](docs/TESTING.md) | E2E verification checklist |
| [AGENTS.md](AGENTS.md) | Contributor conventions for AI-assisted development |

---

## License

BSD-3-Clause — see [LICENSE](LICENSE).
