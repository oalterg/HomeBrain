# HomeBrain WhatsApp Login (OpenClaw plugin)

A tiny first-party OpenClaw plugin that exposes the WhatsApp QR login flow as a
**gateway HTTP route**, so the HomeBrain dashboard can render the QR and poll for
completion against **stock upstream `openclaw`** — no fork required.

```
POST /api/channels/login/whatsapp/start  -> { qrDataUrl, ... }
POST /api/channels/login/whatsapp/wait   -> { qrDataUrl?, ... }
```

## Why this exists

Upstream OpenClaw ships the WhatsApp login *primitives*
(`startWebLoginWithQr` / `waitForWebLogin` in `@openclaw/whatsapp`) and the
`whatsapp_login` *agent tool*, but **not** an HTTP route returning a raw QR data
URL for a web dashboard. The HomeBrain OpenClaw fork added that route as a *core*
gateway file (`src/gateway/channel-login-http.ts`), which forced HomeBrain to run
a custom fork build instead of the npm release.

This plugin re-implements the identical route through the **public plugin SDK**
(`api.registerHttpRoute`, the same mechanism upstream's `webhooks` / `nostr`
extensions use), letting HomeBrain install stock `openclaw` from npm and retire
the fork dependency for channel linking.

`src/integrations.py` (`_gateway_whatsapp_login`) calls these paths unchanged —
the contract is byte-for-byte compatible with the fork route.

## How it works

- Registered with `auth: "gateway"`. The gateway's plugin-http dispatcher
  enforces the gateway bearer token **before** invoking the handler, so the
  handler does no auth itself. HomeBrain injects the token from
  `~/.openclaw/openclaw.json`.
- `activation.onStartup: true` so the route is mounted at gateway boot.
- The WhatsApp login module is loaded lazily on first request; if
  `@openclaw/whatsapp` is not installed the route returns
  `404 {"error":"WhatsApp plugin is not installed"}`.
- **Resolution.** OpenClaw installs each plugin into its own isolated directory
  and symlinks the `openclaw` package into it (so a plugin can
  `import "openclaw/..."`), but it does *not* make sibling plugins resolvable
  from one another. A bare `import("@openclaw/whatsapp/...")` from here therefore
  cannot find the WhatsApp plugin. We resolve it by **absolute path** instead:
  `resolveStateDir()` (from `openclaw/plugin-sdk/state-paths`, honoring
  `OPENCLAW_STATE_DIR` / `OPENCLAW_CONFIG_PATH`) → `<stateDir>/npm/node_modules/
  @openclaw/whatsapp/dist/login-qr-api.js` (npm-spec installs), with the
  `<stateDir>/extensions/...` clawhub/local layouts and the bare specifiers as
  fallbacks. Importing by absolute path is safe: the module's own `openclaw/*`
  imports resolve through the symlink OpenClaw places in the npm dir, so it binds
  to the **same `defaultRuntime` singleton** as the gateway (Node dedupes modules
  by real path).
- Login is self-contained — it opens its own WhatsApp socket, renders the QR, and
  persists credentials to the on-disk auth dir. The running WhatsApp channel
  picks those creds up independently, so driving login from this separate plugin
  is safe.

## Requirements

- `openclaw >= 2026.5.12` (provides `openclaw/plugin-sdk/core` and the
  plugin-http-route dispatcher with `match: "prefix"` + `auth: "gateway"`).
- `@openclaw/whatsapp >= 2026.5.12` installed (provides `dist/login-qr-api.js`).

Both are declared as `peerDependencies`; they are resolved at runtime by the
OpenClaw plugin loader, so this plugin has no installable dependencies of its own.

## Install (handled by provisioning)

`scripts/utilities.sh::setup_openclaw` installs this from the repo:

```bash
openclaw plugins install <repo>/config/openclaw-plugins/homebrain-whatsapp-login --force
```

It is plain ESM JavaScript — no build step.

## The WhatsApp channel plugin (separate from this route)

This plugin only provides the *route*. The actual `@openclaw/whatsapp` channel
plugin (which owns `startWebLoginWithQr` / `waitForWebLogin`) is **not bundled in
stock OpenClaw** and is **not pre-installed** at provisioning. The HomeBrain
dashboard installs it lazily the first time a user links WhatsApp:

- `src/integrations.py::whatsapp_add` runs
  `openclaw plugins install @openclaw/whatsapp@<pin>` in the background (the
  install is too slow for the gunicorn worker timeout) and replies
  `202 {status:"installing"}`; the dashboard polls until it reports
  `configured`, then requests the QR.
- The version is pinned in `config/versions.json` under `openclaw_whatsapp` and
  **must stay peer-compatible** with the `openclaw` pin — a newer WhatsApp build
  can require a newer host (`peerDependencies.openclaw`, `compat.pluginApi`).

Telegram needs none of this: it is bundled in core OpenClaw.

## Compatibility note

This plugin imports `@openclaw/whatsapp/dist/login-qr-api.js`. That subpath and
the `startWebLoginWithQr` / `waitForWebLogin` signatures are effectively public
(the upstream `whatsapp_login` agent tool uses them) but not contractually
frozen. Re-verify on each pinned OpenClaw bump in `config/versions.json`.
