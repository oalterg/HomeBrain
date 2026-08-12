# HomeBrain

**Your private cloud and agent. Nothing leaves the box.**

Nextcloud, Home Assistant, Vaultwarden, and a local AI agent are four products. HomeBrain is the appliance that makes them one: a single password, a backup that restores the whole box, updates that will not brick you, and — on a GPU — an agent that operates it so you never need a shell.

No subscriptions. No cloud accounts. No one else's servers.

A browser wizard configures it. You own the whole thing.

<p align="center">
  <img src="res/screenshot.png" alt="The HomeBrain dashboard: service status, the local AI stack, vault state, and system resources" width="860">
</p>

<p align="center"><sub>Solarized Light, with a dark theme a click away. The dashboard loads nothing from the internet — no CDN, no web fonts, no analytics — so it renders identically on a box with the WAN unplugged.</sub></p>

---

## Why this, not four apps

- **One secret, one recovery** — dashboard, files, smart home, and backup encryption share a master password. A recovery phrase, shown once, resets the lot.
- **An agent that acts** — calendar, lights, files, email. Every token stays on your GPU. Telegram is the daily interface: no VPN, no extra app. The tunnel is optional.
- **Backup that restores the composition** — not four separate dumps. Scheduled, encrypted, off-site, one click from the dashboard.
- **It runs itself** — pinned versions, downgrades blocked, a factory reset back to the wizard. Health alerts while the box is up.

Without a GPU you still get the private cloud and smart-home hub (**HomeCloud**). Same wizard, ARM or x86_64.

---

## The agent's reach

Each integration is one row: connect it, test it, revoke it. The agent talks to every service from there.

<p align="center">
  <img src="res/agent.png" alt="Agent integrations: Home Assistant, Nextcloud, Vault and Email, each with a connection state and per-account rows" width="860">
</p>

---

## Architecture

```
Nextcloud          Docker     Files, calendars, contacts
Home Assistant     Docker     Smart-home automation
Vaultwarden        Docker     Password manager (Bitwarden-compatible)
MariaDB            Docker     Nextcloud + Vault database
Pangolin Newt      Docker     Encrypted tunnel            optional
llama-server       systemd    Local LLM inference         GPU only
whisper-server     systemd    Speech-to-text              GPU only
OpenClaw           systemd    AI agent on Telegram        GPU only
```

Versions are pinned in [`config/versions.json`](config/versions.json) and updated from the dashboard in one click. With an AMD GPU you get the full stack (**HomeBrain**). Without one, the AI services stay off (**HomeCloud**).

---

## Hardware

| | Reference build |
|---|---|
| CPU | AMD Ryzen 5 / Intel i5 or better |
| RAM | 32 GB |
| Storage | 512 GB NVMe |
| GPU | AMD Radeon RX 9060 XT (16 GB VRAM) |
| OS | Ubuntu 24.04 LTS |

Inference is Vulkan via Mesa RADV — no ROCm. ~29 tok/s generation, ~750 tok/s prompt processing at 131K context; see [BENCHMARKS.md](docs/BENCHMARKS.md).

HomeCloud reference: Raspberry Pi 5 (8 GB) with an SSD, Raspberry Pi OS Trixie 64-bit.

---

## Install

```bash
# Ubuntu 24.04+ / Raspberry Pi OS 64-bit
curl -fsSL https://raw.githubusercontent.com/oalterg/HomeBrain/main/install | sudo bash
sudo /opt/homebrain/scripts/provision.sh
sudo reboot
```

`provision.sh` prints a **factory password**. Record it — it is not recoverable, and it is what opens the setup wizard.

After reboot, open `http://<server-ip>`, log in, and choose local network or a Pangolin tunnel. The box deploys itself and shows a generated master password and recovery phrase **once**. On the LAN the dashboard is HTTP; remote access is HTTPS through the tunnel.

Passing tunnel credentials up front does not skip the wizard — it arrives pre-filled:

```bash
sudo /opt/homebrain/scripts/provision.sh \
  --newt-id "<ID>" --newt-secret "<SECRET>" --domain "<TUNNEL_DOMAIN>" \
  --endpoint "<PANGOLIN_ENDPOINT>" --factory-pass "<PASSWORD>"

# force local-network mode
sudo /opt/homebrain/scripts/provision.sh --local --factory-pass "<PASSWORD>"
```

`--factory-pass` sets the wizard login. The master password (dashboard, Nextcloud, Home Assistant) is always generated during deployment and never passed in.

Pick the AI model later, under **Settings → Personal AI Assistant**.

---

## Documentation

| Doc | What's in it |
|-----|-------------|
| [BENCHMARKS.md](docs/BENCHMARKS.md) | Inference throughput, quantization comparisons, tuning notes |
| [DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) | Getting your data back onto new hardware after the box is gone |
| [ROADMAP.md](docs/ROADMAP.md) | Shipped features and what's next |
| [PRODUCT_REVIEW_2026-08.md](docs/plans/PRODUCT_REVIEW_2026-08.md) | Why the glue is the product, and the findings we will act on |
| [TESTING.md](docs/TESTING.md) | E2E verification checklist (including the shared test-box lock) |
| [AGENTS.md](AGENTS.md) | Behavioral rules for AI coding agents |

## License

BSD-3-Clause — see [LICENSE](LICENSE).
