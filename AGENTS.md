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

## Security invariant: the AI agent is root

The OpenClaw agent runs as the `homebrain` OS user, which is in the `docker`
group. Any member of that group can `docker run -v /:/host` and read or write
anything on the box, so **the agent is root-equivalent and always has been.**
As of Phase 5 that is explicit rather than accidental: `tools.exec` is set to
`mode: full` / `ask: off` and `homebrain` has a passwordless sudoers rule,
because "the owner never needs a shell" requires an agent that can actually
run privileged commands.

What this means when reasoning about agent risk:

- **The security boundary is the Telegram `allowFrom` pairing and the
  loopback-bound gateway**, not the tool policy. Anything that lets an
  untrusted party put text in front of the agent is a privilege escalation.
- The agent ingests email and browses the web. Both are prompt-injection
  surfaces that reach a root shell. Treat any change that widens what the
  agent will read as a security change.
- Do not add a filesystem deny-list and call it containment — the agent can
  sudo around it. `tools.deny` (tool names, not paths) is schema-valid and is
  the right tool for disabling a *capability*, not for protecting a *file*.
- **Do not configure `tools.elevated`.** OpenClaw has its own privileged-tool
  surface gated by `tools.elevated.enabled` + `allowFrom`, and when the agent
  tries it the refusal reads like a missing setting ("requires
  `tools.elevated.allowFrom` for the telegram channel"). It is a red herring
  here. HomeBrain's privilege model is *ordinary exec + the host sudoers rule*:
  with `tools.exec.mode=full` and `/etc/sudoers.d/homebrain`, the agent simply
  runs `sudo …` through the normal exec path and is root. Verified on .58
  (2026-07-25): the same command failed with the elevated flag set and
  succeeded without it, reading `/etc/shadow`. Configuring `tools.elevated`
  would add a second, parallel privilege path with its own sender allowlist to
  maintain and get wrong, buying nothing.
- **The two halves must ship together.** `tools.exec.mode=full` (agent stops
  asking for approval) and `ensure_homebrain_sudo` (sudo actually works) are one
  change. A box with only the first has an agent that confidently runs
  privileged commands that all fail; `update.sh` applies both for that reason.
- `.env` is still asserted `root:root 0600` on every write and at manager
  start. That does not stop the agent; it stops a container escape landing as
  `www-data`.

## Repo Layout

```
scripts/           Bash scripts: provision.sh, deploy.sh, backup.sh, restore.sh, update.sh, utilities.sh, common.sh
src/               Flask app (app.py, integrations.py, migration.py, templates/, static/)
                   app.py and integrations.py are both large — read the section you
                   need rather than the whole file. No line count here on purpose:
                   the last one said "~1750" while app.py was past 4,000.
config/            .env.template, platform_models.json, systemd units, udev rules
docs/              BENCHMARKS.md, ROADMAP.md, TESTING.md, plans/
docker-compose.yml Service definitions with profiles (pangolin, cloudflare-nc, cloudflare-ha)
```
