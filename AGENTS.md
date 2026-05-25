# AGENTS.md

Instructions for AI coding agents (Claude, Cursor, opencode, Aider, etc.) contributing to HomeBrain.

HomeBrain is a self-hosted home automation system targeting x86_64 Ubuntu servers with AMD GPUs. It combines an OpenClaw AI assistant (backed by llama.cpp/llama-server), Nextcloud, Home Assistant, and optional Pangolin tunnel for remote access. All services run in Docker; the user interacts exclusively through a Flask dashboard — no SSH required.

## Repo Layout

```
scripts/           Bash scripts: provision.sh, deploy.sh, backup.sh, restore.sh, update.sh, utilities.sh, common.sh
src/               Flask app (app.py ~1750 lines), migration.py, templates/
config/            .env.template, platform_models.json, systemd units, udev rules
docker-compose.yml Service definitions with profiles (pangolin, cloudflare-nc, cloudflare-ha)
ROADMAP.md         Planned features and shipped releases
TESTING.md         E2E verification checklist — follow this before merging
```

## Key Concepts

**`HAS_GPU`** — env var that gates the entire AI stack. Auto-detected at provision time by `detect_gpu()` in `scripts/common.sh`. When set, llama-server is installed and OpenClaw is enabled in the dashboard.

**Deployment modes** — set programmatically by the setup wizard, not edited by hand:
- `DEPLOYMENT_MODE=remote` — Pangolin tunnel active, accessible from the internet
- `DEPLOYMENT_MODE=local` — LAN only, reachable at `homebrain.local`

**AMD GPU power management** — `amdgpu.runpm=0` must remain set (via `config/99-amdgpu-runpm.rules`) to prevent VRAM eviction during inference. Do not remove or weaken this.

**Process model** — long-running operations (setup, backup, restore) spawn background subprocesses. Progress is tracked via a JSON file in `/tmp`; the frontend polls `/api/task_status`.

**Security invariants** (non-negotiable):
- Never use `shell=True` in subprocess calls
- Atomic writes (mkstemp → rename) for sensitive files
- Login endpoint must stay rate-limited
- Session cookies must be `HTTPONLY` + `SAMESITE=Lax`

## Development Workflow

- Active development happens on the `openclaw-integration` branch.
- Do not push directly to `main` — open a PR.
- Commit style: [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`). Subject under 72 characters, imperative mood.

## Testing Requirements

**Follow [TESTING.md](TESTING.md) for all E2E verification.**

Critical rule: Test on real hardware before merging anything that touches provisioning, services, the dashboard, or the AI stack.

## Common Tasks

```bash
# Run the dashboard locally (venv must be active)
source /opt/homebrain/venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 src.app:app   # http://localhost:8000

# Provision a fresh device (run as root or with sudo)
sudo bash scripts/provision.sh

# Run a backup
sudo bash scripts/backup.sh
```

## Agent Operating Principles

- **High agency** — Use every tool available. Parallelize independent work. Drive tasks to completion without unnecessary hand-holding.
- **Research actively** — Verify against current source, docs, and runtime behavior. Do not rely on stale assumptions.
- **Think top-down** — Periodically step back and re-evaluate architecture with first-principles reasoning.
- **Persist** — Once a task is started, continue until it is genuinely complete or a hard blocker is reached that requires human input.
- **Work cleanly** — Document key decisions and observations as you go.
- **Bias to action** — Do not ask for permission or feedback on every step. Execute.

### Autonomy Guidance

Do not pause to ask "should I keep going?" or "is this a good stopping point?" unless you have hit a true blocker (missing credentials, irreversible decision, or information you cannot derive). The goal is to deliver complete, working results.

## Security & Correctness Invariants

These rules are called out in multiple planning docs and must be upheld:

- No `shell=True` anywhere in provisioning or MCP scripts (see VAULT_PLAN.md, INTEGRATIONS_PLAN.md).
- All token/secret files: mode `0600`, owned by the `homebrain` user.
- Atomic file updates for any sensitive configuration.
- Rate limiting on all authentication and privileged endpoints.

When in doubt, re-read the relevant sections of `app.py`, `scripts/common.sh`, and the security checklists in the plan documents.
