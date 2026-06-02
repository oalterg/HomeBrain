/**
 * HomeBrain WhatsApp Login — OpenClaw plugin
 * ------------------------------------------------------------------
 * Exposes the WhatsApp QR login flow as a gateway HTTP route so the
 * HomeBrain Flask dashboard can render the QR in a plain <img> and poll
 * for completion — no LLM turn, no MEDIA: directive, no reverse-proxy
 * round-trip.
 *
 * WHY THIS EXISTS
 *   Upstream OpenClaw ships the WhatsApp *login primitives*
 *   (`startWebLoginWithQr` / `waitForWebLogin` in @openclaw/whatsapp) and
 *   the `whatsapp_login` *agent tool*, but NOT an HTTP route that returns
 *   a raw QR data URL. The HomeBrain fork added that route as a core
 *   gateway file (src/gateway/channel-login-http.ts). This plugin
 *   re-implements the exact same route via the public plugin SDK
 *   (`api.registerHttpRoute`), so HomeBrain can install stock upstream
 *   `openclaw` from npm and drop the fork entirely.
 *
 * CONTRACT (kept byte-for-byte compatible with the fork route so the
 * dashboard's existing calls in src/integrations.py work unchanged):
 *   POST /api/channels/login/whatsapp/start  -> { qrDataUrl, ... }
 *   POST /api/channels/login/whatsapp/wait   -> { qrDataUrl?, ... }
 *
 * AUTH
 *   Registered with auth:"gateway". The gateway's plugin-http dispatcher
 *   enforces the gateway bearer token BEFORE invoking this handler, so the
 *   handler itself performs no auth (unlike the fork's core route, which
 *   had to authorize inline). HomeBrain injects the gateway token from
 *   ~/.openclaw/openclaw.json.
 *
 * STATE MODEL
 *   The login flow is self-contained: it opens its own WhatsApp socket,
 *   renders the QR, and persists credentials to the on-disk auth dir. The
 *   running WhatsApp channel picks those creds up independently, so driving
 *   login from this separate plugin is safe — the only shared in-memory
 *   state (the active-QR map) lives within this module instance, which is
 *   exactly the start->wait pairing we need.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/core";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const CHANNEL_LOGIN_PREFIX = "/api/channels/login/";

// ---- tiny HTTP helpers (ported from the fork's http-common.ts) ----------

function sendJson(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
}

function sendText(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.end(body);
}

function sendMethodNotAllowed(res, allow = "POST") {
  res.setHeader("Allow", allow);
  sendText(res, 405, "Method Not Allowed");
}

function readJsonBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString()) || {});
      } catch {
        resolve({});
      }
    });
    req.on("error", () => resolve({}));
  });
}

// ---- lazy WhatsApp login module ----------------------------------------
// Loaded on first request so the gateway never fails at startup when the
// @openclaw/whatsapp channel plugin is not installed (it returns 404 instead).
//
// RESOLUTION — the subtle part. OpenClaw installs each plugin into its OWN
// isolated directory and symlinks the *openclaw* package into it so the plugin
// can `import "openclaw/..."`. It does NOT make sibling plugins resolvable from
// one another. So a bare `import("@openclaw/whatsapp/...")` from THIS plugin's
// dir will not find the WhatsApp plugin — they live in separate trees.
//
// We therefore resolve @openclaw/whatsapp by absolute path. npm-spec channel
// plugins install into `<stateDir>/npm/node_modules/<pkg>`; clawhub/local
// installs land under `<stateDir>/extensions/`. We compute <stateDir> with
// OpenClaw's own `resolveStateDir` (imported lazily so an older host that lacks
// the subpath only loses the fallback, not the whole plugin), which honors
// OPENCLAW_STATE_DIR / OPENCLAW_CONFIG_PATH exactly as the gateway does.
//
// Importing the WhatsApp login module by absolute path is safe: its own
// `openclaw/*` imports resolve through the openclaw symlink OpenClaw places in
// the npm dir, so it binds to the SAME `defaultRuntime` singleton as the
// gateway (Node dedupes modules by real path).

let whatsappLoginModule;

async function tryImportLogin(specifier) {
  try {
    const mod = await import(specifier);
    return mod && typeof mod.startWebLoginWithQr === "function" ? mod : null;
  } catch {
    return null;
  }
}

async function whatsappLoginCandidates() {
  const files = [];
  try {
    const { resolveStateDir } = await import("openclaw/plugin-sdk/state-paths");
    const stateDir = resolveStateDir();
    const rel = path.join("@openclaw", "whatsapp", "dist", "login-qr-api.js");
    files.push(
      // npm-spec install (deterministic, what HomeBrain provisioning uses).
      path.join(stateDir, "npm", "node_modules", rel),
      // clawhub / local-dir installs land in the extensions dir.
      path.join(stateDir, "extensions", rel),
      path.join(stateDir, "extensions", "whatsapp", "dist", "login-qr-api.js"),
    );
  } catch {
    // state-paths not available on this host — rely on the bare fallback below.
  }
  return files;
}

async function loadWhatsAppLogin() {
  if (whatsappLoginModule !== undefined) return whatsappLoginModule;

  // 1) Absolute paths derived from OpenClaw's state dir (the real location).
  for (const file of await whatsappLoginCandidates()) {
    if (!existsSync(file)) continue;
    const mod = await tryImportLogin(pathToFileURL(file).href);
    if (mod) return (whatsappLoginModule = mod);
  }

  // 2) Bare specifiers — harmless forward-compat in case a future host wires
  //    peer-plugin resolution, or @openclaw/whatsapp is otherwise on our path.
  for (const spec of [
    "@openclaw/whatsapp/dist/login-qr-api.js",
    "@openclaw/whatsapp/login-qr-api.js",
  ]) {
    const mod = await tryImportLogin(spec);
    if (mod) return (whatsappLoginModule = mod);
  }

  whatsappLoginModule = null;
  return whatsappLoginModule;
}

// ---- route handler ------------------------------------------------------

async function handleWhatsAppLogin(req, res) {
  const requestUrl = new URL(req.url ?? "/", "http://localhost");

  if (req.method !== "POST") {
    sendMethodNotAllowed(res, "POST");
    return;
  }

  // action is e.g. "whatsapp/start" / "whatsapp/wait" (mirrors the fork).
  const action = requestUrl.pathname.startsWith(CHANNEL_LOGIN_PREFIX)
    ? requestUrl.pathname.slice(CHANNEL_LOGIN_PREFIX.length)
    : "";

  if (action !== "whatsapp/start" && action !== "whatsapp/wait") {
    sendText(res, 404, "unknown channel login action");
    return;
  }

  const mod = await loadWhatsAppLogin();
  if (!mod) {
    sendJson(res, 404, { ok: false, error: "WhatsApp plugin is not installed" });
    return;
  }

  const body = await readJsonBody(req);

  try {
    if (action === "whatsapp/start") {
      const result = await mod.startWebLoginWithQr({
        force: body.force === true,
        timeoutMs: typeof body.timeoutMs === "number" ? body.timeoutMs : 30_000,
      });
      sendJson(res, 200, result);
    } else {
      const result = await mod.waitForWebLogin({
        timeoutMs: typeof body.timeoutMs === "number" ? body.timeoutMs : 120_000,
        currentQrDataUrl:
          typeof body.currentQrDataUrl === "string" ? body.currentQrDataUrl : undefined,
      });
      sendJson(res, 200, result);
    }
  } catch (err) {
    sendJson(res, 500, {
      ok: false,
      error: err instanceof Error ? err.message : String(err),
    });
  }
}

// ---- plugin entry -------------------------------------------------------

export default definePluginEntry({
  id: "homebrain-whatsapp-login",
  name: "HomeBrain WhatsApp Login",
  description:
    "Gateway HTTP route for WhatsApp QR linking, consumed by the HomeBrain dashboard.",
  register(api) {
    api.registerHttpRoute({
      // Prefix match so /api/channels/login/whatsapp/{start,wait} both hit
      // this handler; the handler routes on the trailing action segment.
      path: "/api/channels/login/whatsapp",
      match: "prefix",
      auth: "gateway",
      handler: handleWhatsAppLogin,
    });
  },
});
