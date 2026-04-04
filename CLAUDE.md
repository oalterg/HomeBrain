# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

HomeBrain is a self-hosted private cloud server automation platform for Raspberry Pi 5. It automates deployment of Nextcloud and Home Assistant in Docker containers, accessible via encrypted tunnels (Pangolin or Cloudflare). There is no vendor lock-in; all data stays on-device. User manages all functionality via the dashboard GUI.

## Running the Application

The Flask manager runs on port 8000 via Gunicorn, managed by a systemd service:

```bash
# Production (systemd)
sudo systemctl start homebrain-manager

# Direct dev run (from repo root, with venv active)
source /opt/homebrain/venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 src.app:app
```

There are no build steps, no transpilation, no Makefile.

## Architecture

### Process Model

Long-running operations (setup, backup, restore, upgrades) are launched as background subprocesses from Flask routes. Progress is tracked via a JSON status file in `/tmp` with in-memory fallback. The frontend polls `/api/task_status` to display progress.

### Flask App (`src/app.py`)

Single large Flask file (~1750 lines) containing all API routes, authentication, and system management logic. Key architectural patterns:

- **Session auth** with rate limiting (Flask-Limiter): 2000 req/min general, 5/min on `/login`
- **Subprocess spawning** (never `shell=True`) for Bash script execution
- **Atomic file writes** (mkstemp + rename) and `fcntl` locking for concurrent safety
- **One-time credential handover**: setup credentials written to a staging file, read once via `/api/setup/credentials`, then deleted

### Docker Compose Profiles

Services are activated by profile:
- (default/no profile) — `db`, `redis`, `nextcloud`, `homeassistant`
- `pangolin` — Newt tunnel container
- `cloudflare-nc`, `cloudflare-ha` — Cloudflare tunnel containers

### Bash Scripts (`scripts/`)

- `provision.sh` — One-time device setup: installs Docker, Python venv, pulls images, writes factory config to `/boot/firmware/factory_config.txt`
- `deploy.sh` — Starts/reconfigures the Docker stack
- `common.sh` — Shared utilities (health checks, env loading, Docker helpers) sourced by other scripts
- `backup.sh` / `restore.sh` — Data persistence for Nextcloud and Home Assistant
- `update.sh` — Self-update logic for the Manager app
- `utilities.sh` — System operations: Home Assistant admin account creation, Nextcloud cron, FTP, Zigbee

### Configuration

Environment variables live in `/opt/homebrain/.env` (generated from `config/.env.template`). Factory provisioning parameters are stored in `/boot/firmware/factory_config.txt`. Logs go to `/var/log/homebrain/`.

## Security Constraints

- Never use `shell=True` in subprocess calls — always pass commands as lists
- File writes to sensitive paths must use atomic pattern (mkstemp → rename) with `chmod 0o600`
- Login endpoint must remain rate-limited
- Session cookies must remain HTTPONLY + SAMESITE=Lax

## Migrations (`src/migration.py`)

Runs on app startup to handle version drift (e.g., syncing the systemd service file from the repo, renaming legacy cron jobs). Add new migrations here when deployments need one-time fixups.

## Contributing

### Branching & PRs
- Never commit directly to `main` — create a feature branch and merge via pull request
- Branch names should be descriptive: `feature/openclaw-integration`, `fix/backup-timeout`
- PRs require review before merge; keep them focused on a single concern

### Commit Style
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`
- Subject line under 72 characters, imperative mood ("add X" not "added X")
- Body explains *why*, not *what* (the diff shows what)

### Testing
- Test on the target Raspberry Pi 5 before signing off work — the dev machine is not the deployment environment
- Verify dashboard interactions end-to-end (button click → background task → status update)
- For bash scripts: test both fresh-install and re-run (idempotency) paths

### General
- Aim for robustness and user-friendliness — the user should never need to SSH
- Ask when uncertain rather than guessing
- Think critically and give open feedback on the approach, not just the implementation