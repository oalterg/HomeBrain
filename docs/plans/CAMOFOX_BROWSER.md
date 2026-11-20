# Camofox browser

Status: built, 2026-11-20. It replaces OpenClaw's Chrome-driven browser tool on AI boxes. A hand-installed trial ran on .69 from 2026-11-19.

## Goal

The agent can read sites that block automated browsers, such as Google, Reddit and Cloudflare-protected pages, under three conditions:
- No third party sees what it browses.
- The browser can't take over the box.
- It costs no more prompt than the Chrome tool did.

## Decisions

1. **The browser is a container built on the box,** like Proton Bridge.
   - Its image is `homebrain-camofox`, built from `config/camofox/` with the pins in `config/versions.json`.
   - Chrome used to run on the host as `homebrain`, a docker-group user, so a browser exploit there was root on the box.
2. **HomeBrain ships its own MCP server,** `scripts/mcp-browser.py` (`homebrain-browser`), not the upstream OpenClaw plugin.
   - 11 tools: `browser.open`, `snapshot`, `click`, `type`, `press`, `select`, `scroll`, `back`, `wait`, `screenshot`, `close`.
   - It always uses one fixed browser user and session, so the model never fills those in.
   - The upstream plugin's `autoStart` path can't turn crash reports off. It never runs.
3. **camofox-browser runs unmodified.**
   - The MCP server re-windows snapshots to 24,000 characters itself, the same way camofox's `windowSnapshot()` does it at 80,000.
   - That fits OpenClaw's 32,000-character cap per tool result, which drops the middle of anything longer.
   - It checks the server's window shape and falls back, saying so, if a camofox-browser bump changes it.
4. **Camoufox comes from its author's release** (`daijro/camoufox`), checked against the pinned sha256 at build time.
   - Upstream's Dockerfile downloads it with no checksum.
   - During daijro's outage on 2026-11-19, jo-inc's mirror carried the same file: byte-identical, same sha256.
5. **The version is 152.0.4-beta.30,** although daijro has 158.0.1-beta.41.
   - camofox-browser 1.19.0 calls 152.0.4-beta.30 "the latest stable browser release tested with camoufox-js@0.11.5" and notes that beta.34 rejects `navigator.product`.
   - Move to a newer Camoufox only with a camofox-browser release that supports it.
6. **Owner decisions (2026-11-20):**
   - Camofox replaces Chrome everywhere.
   - Raspberry Pis don't get it, since they get no AI stack.

## How it fits together

- **`ensure_camofox`** (`scripts/common.sh`) runs on AI boxes: in `setup_openclaw`, and in `update.sh` before the OpenClaw refresh.
  - It builds the image when the tag for the current pins (`<browser>-<camoufox>-<release>`) is missing, tags it `:current`, starts it, and removes old tags.
  - Then it purges the Chrome or Chromium HomeBrain had installed.
  - A failed build is a warning. The box keeps the browser it had.
- **`get_compose_args`** adds `--profile camofox` once `homebrain-camofox:current` exists.
  - Every compose call goes through it, so no `up --remove-orphans` path stops the browser.
  - The service has no `build:` and uses `pull_policy: never`, so `up` never builds it.
- **The `camofox` service:**
  - published on `127.0.0.1:9377` only, on its own network, so it can't reach the database or Redis by name;
  - no volumes, `cap_drop: ALL`, `no-new-privileges`, 2 GB memory limit;
  - `CAMOFOX_ACCESS_KEY`, generated per box into `.env`, is required on every request;
  - crash reports off.
- **Wiring:**
  - `SPEC_BUILDERS["browser"]` returns a spec only while the image and the key both exist.
  - `_startup_wire_if_needed` wires or unwires it when the manager starts, and `update.sh` restarts the manager after building.
  - The browser isn't a Connections card.
- **OpenClaw config:** with the image present, `patch_openclaw_config` turns OpenClaw's own `browser` plugin off, drops `.browser`, and removes `browser` from the main agent's `alsoAllow`.
- **Audit:** `browser.open` writes the URL or search to `mcp-browser-audit.log`, and the Logs tab shows it under Agent activity.

## Measured on .69 (x86_64), 2026-11-20

- **Build:** 2 min 46 s.
- **Image:** 3.16 GB, of which Camoufox unpacked is 1.29 GB.
- **Memory:** 418 MiB idle, 537 MiB after a browsing run.
- **Sites:** Google search returned real results with no captcha, Reddit loaded, and nowsecure.nl (Cloudflare) passed.
- **Paging:** a 453k-character Wikipedia article read in windows 0, 21800, 43600 and 65400, each result 24,074 characters.
- **Tools:** all 11 work over stdio. Paging right after a click retries correctly.
- **Network** (DNS replies captured in the container's namespace):
  - 60 s idle: no lookups at all.
  - Browsing: only the sites visited and what their pages load.
  - Nothing to Mozilla, askjo or Sentry.
  - Names resolve through Docker's resolver. Camoufox has no DNS-over-HTTPS settings.
- **`tools/list`:** 4,053 characters for the 11 tools.

## Risks

- **Camoufox is a beta Firefox fork,** pinned at 152, and its security fixes trail Firefox's. The container is the mitigation, not a fix.
- **The browser can reach the LAN,** like any browser on it. A page can make it fetch local addresses.
- **Upstream churn.** On a camofox-browser bump, check `SERVER_CHARS`/`SERVER_TAIL` in `mcp-browser.py` against `lib/snapshot.js`, and check which Camoufox version it supports.
- **Untrusted pages** can carry prompt injection. The workspace `AGENTS.md` treats browser content as untrusted data.
- **DataDome sites** still block.
