/**
 * HomeBrain dead-man's switch — the half that does NOT run on the box.
 *
 * Every other alert HomeBrain sends is sent by HomeBrain. A box that is off,
 * unplugged, stolen, or whose disk has died sends nothing, and silence is
 * indistinguishable from healthy. This is the only alert that works when the
 * box does not, and it is the one whose failure mode is "you find out in three
 * weeks".
 *
 * Two entry points:
 *   POST /heartbeat   — the box says "still here" on its health timer.
 *   scheduled()       — a cron trigger looks for boxes that have gone quiet
 *                       and emails their owners once.
 *
 * ── NOT DEPLOYED ───────────────────────────────────────────────────────────
 * This file is the source to deploy, not a deployed service. It needs a
 * Cloudflare account that is not the agent's to touch. To bring it up:
 *
 *   1. Create a KV namespace and bind it as HEARTBEATS.
 *   2. Merge these two exports into the existing registrar Worker (it already
 *      has REGISTRAR_SECRET and the Zitadel credentials), or deploy separately
 *      and point HEARTBEAT_URL at it.
 *   3. Add a cron trigger in wrangler.toml:
 *        [triggers]
 *        crons = ["0 * * * *"]
 *   4. Set MAIL_FROM and either wire sendAlert() to your existing mail
 *      provider or to Zitadel, whichever you already pay for.
 *   5. On each box: set HEARTBEAT_ENABLED=true in .env (or HEARTBEAT_URL for a
 *      separate deployment). The device half ships disabled precisely because
 *      this half may not exist yet — a switch that silently does nothing is
 *      worse than no switch.
 *
 * The device half lives in scripts/healthcheck.py::send_heartbeat.
 */

// How long a box may stay quiet before the owner hears about it. The box beats
// on its health timer (every 6h by default), so this tolerates two missed
// beats plus slack — long enough that a reboot or a brief outage is not an
// alarm, short enough that a dead box is not a three-week secret.
const QUIET_HOURS = 26;

// Once per outage, not once per cron tick. A dead box stays dead, and a daily
// reminder about it is how the owner learns to filter these.
const REMIND_HOURS = 24 * 7;

export async function handleHeartbeat(request, env) {
  const auth = request.headers.get("Authorization");
  if (!auth || auth.trim() !== `Bearer ${env.REGISTRAR_SECRET}`) {
    return new Response(JSON.stringify({ error: "Unauthorized" }), {
      status: 401, headers: { "Content-Type": "application/json" },
    });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  const deviceId = String(body.device_id || "").slice(0, 128);
  if (!deviceId || deviceId === "unknown") {
    return new Response(JSON.stringify({ error: "device_id required" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  // Merge rather than overwrite: the email is written at registration and the
  // box does not send it, so a naive put() would erase the only way to reach
  // the owner on the very first heartbeat.
  const key = `device:${deviceId}`;
  const prev = await env.HEARTBEATS.get(key, { type: "json" }) || {};
  await env.HEARTBEATS.put(key, JSON.stringify({
    ...prev,
    last_seen: Date.now(),
    overall: body.overall || "unknown",
    alerted_at: 0,          // it is alive; re-arm the alert
  }));

  return new Response(JSON.stringify({ status: "ok" }), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

export async function checkForSilence(env) {
  const now = Date.now();
  const quietMs = QUIET_HOURS * 3600 * 1000;
  const remindMs = REMIND_HOURS * 3600 * 1000;

  let cursor;
  do {
    const page = await env.HEARTBEATS.list({ prefix: "device:", cursor });
    for (const entry of page.keys) {
      const rec = await env.HEARTBEATS.get(entry.name, { type: "json" });
      if (!rec || !rec.email || !rec.last_seen) continue;

      const quietFor = now - rec.last_seen;
      if (quietFor < quietMs) continue;
      if (rec.alerted_at && now - rec.alerted_at < remindMs) continue;

      const hours = Math.floor(quietFor / 3600000);
      await sendAlert(env, rec.email, hours);
      await env.HEARTBEATS.put(entry.name,
        JSON.stringify({ ...rec, alerted_at: now }));
    }
    cursor = page.cursor;
  } while (cursor);
}

async function sendAlert(env, email, hours) {
  // Deliberately plain. This message reaches someone whose home server may
  // have been stolen or may simply be unplugged, and it must not read as an
  // emergency when the likeliest cause is a holiday power cut.
  const subject = "Your HomeBrain has been quiet";
  const text =
    `Your HomeBrain has not checked in for about ${hours} hours.\n\n` +
    `That usually means it is switched off, has lost its internet connection, ` +
    `or has been unplugged. It can also mean the machine has failed.\n\n` +
    `If you did not expect this, it is worth checking on it — while a box is ` +
    `off it is not making backups.\n\n` +
    `You will not get another message about this for a week.`;

  // Wire this to whatever you already send registration mail through. Left as
  // one obvious function rather than a provider abstraction: there is one
  // provider, and a second one is a reason to change this line, not to build
  // an interface today.
  await fetch(env.MAIL_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${env.MAIL_TOKEN}`,
    },
    body: JSON.stringify({ from: env.MAIL_FROM, to: email, subject, text }),
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/heartbeat") {
      return handleHeartbeat(request, env);
    }
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(checkForSilence(env));
  },
};
