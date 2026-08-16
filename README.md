# HomeBrain

**Your private local cloud, agent wired into everything.**

Nextcloud, Home Assistant, Vaultwarden, and a local AI agent are four products. HomeBrain is the appliance that makes them one: a single password, a backup that restores the whole box, updates that will not brick you, and — on a GPU — an agent that operates it so you never need a shell.

No subscriptions. No cloud accounts. No one else's servers.

A browser wizard configures it. You own the whole thing.

<p align="center">
  <img src="res/screenshot.png" alt="The HomeBrain dashboard: service status, the local AI stack, vault state, a self-test, and system resources" width="860">
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

Each integration is one row: connect it, test it, revoke it. The agent talks to every service from there. More than one account per service is fine — two homes, two Nextcloud users, several mailboxes.

<p align="center">
  <img src="res/agent.png" alt="Agent integrations — Home Assistant, Nextcloud, Vault and Email, each with a connection state and per-account rows — above the messaging channel that carries the agent" width="860">
</p>

<p align="center"><sub>Below the integrations sits the other half: the messenger the agent answers on. Pairing is a code, not a port.</sub></p>

---

## The model is a setting

<p align="center">
  <img src="res/assistant.png" alt="The Personal AI Assistant settings: a model dropdown, and the downloaded models with their sizes, one marked in use" width="860">
</p>

<p align="center"><sub>Download a model, switch to it, delete the ones you are not using. The previous model stays on disk, so switching back is instant. Weights are fetched once, from the dashboard — never at inference time.</sub></p>

---

## Where the backup goes

<p align="center">
  <img src="res/backup.png" alt="The Backup and Storage tab: schedule and retention, the backup drive and disk usage, and an off-site copy mid-upload" width="860">
</p>

<p align="center"><sub>A schedule, a drive, and somewhere off-site — the whole backup story is one tab. Archives are encrypted with your master password before they leave the box, so the target never sees your data. WebDAV, SFTP or S3 — or a second HomeBrain, which hands out a dedicated <code>replica</code> account for exactly this.</sub></p>

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
```

`provision.sh` prepares the host and starts the manager. The setup wizard is available as soon as it finishes. On AMD GPU hardware a reboot is recommended so kernel parameters take effect; it is not required to open the wizard.

If no factory password was supplied, `provision.sh` generates one and prints it once. Record it — it opens the wizard. It is stored in `factory_config.txt` (mode 600); the script will not print it again. `--factory-pass` sets it instead of generating one.

Open `http://<server-ip>`, log in, and initialize. Local network is the default. On a GPU box, pair Telegram after setup — that is how you reach the agent from a phone. A public tunnel is optional. The wizard shows the generated master password and recovery phrase **once**. (Restoring from backup reuses the old master password instead of generating one.)

On the LAN the dashboard is HTTP. Nextcloud and Vault also speak HTTPS on local ports.

**HomeCloud** (no GPU) has no agent, so a [Pangolin](docs/PANGOLIN.md) tunnel is how you reach the browser from outside. Passing credentials at provision time does not skip the wizard; they are stored on the box and the HomeCloud wizard defaults to using them:

```bash
sudo /opt/homebrain/scripts/provision.sh \
  --newt-id "<ID>" --newt-secret "<SECRET>" --domain "<TUNNEL_DOMAIN>" \
  --endpoint "<PANGOLIN_ENDPOINT>" --factory-pass "<PASSWORD>"
```

On an already-set-up box the same command repoints the live tunnel without wiping data.

<p align="center">
  <img src="res/tunnel.png" alt="Tunnel settings: endpoint, device ID and main domain, with a preview of the manager, Nextcloud and Home Assistant subdomains" width="860">
</p>

<p align="center"><sub>One domain in, three subdomains out — no port forwarding and no inbound firewall rule. The same screen moves the box to a Cloudflare Tunnel, or reverts every field to what the device shipped with.</sub></p>

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
| [PANGOLIN.md](docs/PANGOLIN.md) | Optional browser tunnel (HomeCloud, or GPU box web UIs from outside) |
| [AGENTS.md](AGENTS.md) | Behavioral rules for AI coding agents |

## License

BSD-3-Clause — see [LICENSE](LICENSE).
