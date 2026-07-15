# AGENTS.md

Behavioral Guidelines for Agents.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

## Project
HomeBrain is a self-hosted home automation system targeting x86_64 Ubuntu servers with AMD GPUs. It combines an OpenClaw AI assistant (backed by llama.cpp/llama-server), Nextcloud, Home Assistant, and optional Pangolin tunnel for remote access. All services run in Docker; the user interacts exclusively through a Flask dashboard — no SSH required.

## Repo Layout

```
scripts/           Bash scripts: provision.sh, deploy.sh, backup.sh, restore.sh, update.sh, utilities.sh, common.sh
src/               Flask app (app.py ~1750 lines), migration.py, templates/
config/            .env.template, platform_models.json, systemd units, udev rules
docs/              BENCHMARKS.md, ROADMAP.md, TESTING.md, plans/
docker-compose.yml Service definitions with profiles (pangolin, cloudflare-nc, cloudflare-ha)
```
