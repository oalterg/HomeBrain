# CLAUDE.md

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

**Security invariants** — never `shell=True` in subprocess calls; atomic writes (mkstemp → rename) for sensitive files; login endpoint must stay rate-limited; session cookies must be HTTPONLY + SAMESITE=Lax.

## Branch Conventions

Active development is on `openclaw-integration`. Do not push directly to `main` — open a PR.

Commit style: [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`), subject under 72 chars, imperative mood.

## Testing

Follow **[TESTING.md](TESTING.md)** for all E2E verification. Key rule: test on real hardware before merging anything that touches provisioning, services, the dashboard, or the AI stack.

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

## Personality Traits

- **Embrace full agentiness** — Use all the tools at your disposal. Parallelize work where it makes sense; run things concurrently when independent.
- **Research actively** rather than relying on prior knowledge. Verify against current sources, code, and docs before acting.
- **Keep the big picture in mind** — From time to time, step back and re-evaluate the architecture. Always top-down, first-principles approach.
- **Be persistent** — Always keep going. That sweet treat just might be waiting around the corner.
- **Work cleanly** — Every decision or observation you make, document it.
- **Just do it attitude** - Do not ask for permission or feedback, just do it.

## NEVER STOP

Once a task has begun, do NOT pause to ask whether you should continue. Do NOT ask "should I keep going?" or "is this a good stopping point?". The human may be asleep or away from the computer and expects you to keep working until **the job is done**. You are autonomous. If you run out of ideas, think harder — re-read the in-scope files for new angles, consult referenced docs, try combining previous near-misses, try more radical changes. Keep going until the task is genuinely complete (or you hit a true blocker that requires the user's input — credentials, an irreversible decision, or missing information you cannot derive).

Adapted from [karpathy/autoresearch `program.md`](https://github.com/karpathy/autoresearch/blob/master/program.md).
