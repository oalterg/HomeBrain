# HA watchers — ping, wake, and agent-written automations

**Status:** P1–P5 in tree (2026-08-17). Wake CLI is `openclaw agent
--session-key ha-watch --channel … --to … --deliver` (docs; confirm on
the box that it does not steal the main Telegram session). Photo ping
uses `openclaw message send --media` (no llama).
**Date:** 2026-08-16
**Essence:** HomeBrain holds an outbound Home Assistant websocket per MCP
account. On a watched `state_changed`, it **pings** Telegram with no LLM
(photo if asked) and may **wake** the agent as a clerk. Actuators (light,
siren) are HA automations on the box that owns the hardware. The owner
configures both from Telegram. Local HA and a remote HA are the same object
with a different `base_url`.

Related: [`INTEGRATIONS_PLAN.md`](INTEGRATIONS_PLAN.md) (historical MCP
shape), [`INBOUND_AGENT_CONTENT.md`](INBOUND_AGENT_CONTENT.md) (wake prompts
treat HA names as data), `scripts/healthcheck.py` (`openclaw message send`),
`scripts/mcp-homeassistant.py` (camera proxy, not `camera.snapshot`),
`scripts/mcp-homebrain.py` (self MCP — where watcher CRUD lives).

---

## 1. Goals & non-goals

### Goals

- One Telegram sentence such as: *when the front cam sees a person, ping me
  and turn on the light* becomes two writes: a HomeBrain watcher (ping ±
  wake) and an HA automation (the light) on that account.
- Same path for HA on this box (`ws://127.0.0.1:8123/api/websocket`) and a
  remote account (`wss://ha.example.com/api/websocket`). No `is_remote`
  flag. No Pangolin on the GPU box.
- Ping survives llama being busy or down. Wake is extra, never the floor
  that delivers the still.
- Agent-driven config. The owner does not edit JSON.

### Non-goals

- Not IFTTT, MQTT, cron-in-JSON, email triggers, or a public inbound
  webhook (that *would* need Pangolin here).
- Not a replacement for `healthcheck.py` or the OpenClaw hourly heartbeat.
  Internal events (backup finished) call `send_push` directly if needed.
- Not putting the loop inside an MCP server. MCP is a short-lived stdio
  subprocess; watchers must run while nobody is chatting.
- Not `camera.snapshot` (file lands on the remote HA). Not writing
  `automations.yaml` via `exec`.
- Not a dashboard editor in v1. Not a second household agent.
- Not using the 35B as a person detector or to “see” the JPEG. Shipped
  models do not load mmproj. Wake is tool-use and a short follow-up, not
  vision.

---

## 2. Split of responsibility

| Job | Where | Why |
|---|---|---|
| Person / door / washer **event** | HA event bus → HomeBrain websocket | Native trigger, local or WAN |
| Telegram **ping** (+ still) | `homebrain-ha-watch.service` | No LLM; same primitive as healthcheck |
| **Wake** agent | Isolated OpenClaw turn, after ping | Clerk: other cameras, alarm state, *ask* about siren |
| Light / siren / lock / valve | HA automation **on that account** | Works if this GPU or the WAN to the remote HA is down |
| Create/list/delete watchers | Self MCP `homebrain.watcher_*` | Config lives on HomeBrain |
| Create/edit/delete HA automations | HA MCP `ha.automation_*` wrapping `/api/config/automation/config/<id>` | UI store, not the service-call API |
| Lookup entities | Existing `ha.entity_search` | Agent must not invent entity ids |

The service-call API cannot create automations. The agent was right.
`ha.call_service` with domain `automation` only enables/triggers/reloads
what already exists. Upsert uses the same config endpoint the HA frontend
uses (undocumented, default `config` integration, LLAT). Scripts
(`/api/config/script/config/<id>`) are the same shape and a smaller blast
radius if we need a stepping stone.

**Do not** put `light.turn_on` or `siren.turn_on` on the watcher. Two
failure domains on purpose.

---

## 3. Runtime

One systemd unit, GPU / `openclaw` present only: `homebrain-ha-watch.service`,
user `homebrain`.

One process, **one websocket per HA account that has at least one enabled
watcher**. Auth with the Fernet-decrypted LLAT already in
`~/.openclaw/ha_accounts.json` (`HOMEBRAIN_INTEGRATIONS_KEY`, same as the
HA MCP). Then `subscribe_events` / `state_changed`. Filter to watched
`entity_id`s in process.

- Local: `http://127.0.0.1:8123` → `ws://127.0.0.1:8123/api/websocket`
- Remote: `https://ha.example.com` → `wss://ha.example.com/api/websocket`
- Account down: reconnect that socket with backoff. Do not stall the
  other account.
- Keepalive: HA websocket ping/pong. Log and reconnect on silence.

Python stdlib has no websocket client. Pin a small library (e.g.
`websocket-client`) or reuse whatever the box already has. Do not vendor
RFC6455. Do not import `mcp-homeassistant.py` into the daemon (it is a
stdio server). Duplicate the few dozen lines of camera-proxy GET, size
cap, and write-to-workspace, or extract a tiny `ha_http.py` used by both
— only if the second copy actually hurts.

On `state_changed` for a watcher, after cooldown, if `old`/`new` are real
states (never `unavailable` / `unknown`) and `new == to`:

1. Fetch still if `camera_entity_id` is set (camera proxy onto this box).
2. **Ping:** `openclaw message send` to the paired Telegram, plus photo if
   we have one. This is the floor.
3. **Wake** only if `wake: true` (see §5). Isolated session. Facts only.

**Cold start / restore / crash:** seed `last_state` from current HA state
and **do not fire**. Empty `ha_watch_state.json` must not look like every
entity just turned on.

State file: `/var/lib/homebrain/ha_watch_state.json`. Reload
`ha_watchers.json` each event (or on inotify). MCP writes are atomic +
flock.

Cooldown is per watcher, default 120s, covering ping and wake together.

---

## 4. Watcher schema

`~/.openclaw/ha_watchers.json` mode 0600, backed up with
`ha_accounts.json`.

```json
{
  "id": "front-person",
  "enabled": true,
  "ha_account": "remote",
  "entity_id": "binary_sensor.front_person",
  "to": "on",
  "cooldown_s": 120,
  "message": "Person at the front",
  "camera_entity_id": "camera.front",
  "wake": true
}
```

That is the whole schema. No action graph, no `siren:`, no cron, no
attributes. Same `id` on `watcher_set` replaces. `ha_account` must exist
in `ha_accounts.json`. Ignore attribute-only updates; compare `state`
only.

---

## 5. Ping vs wake

| | Ping | Wake |
|---|---|---|
| LLM | no | yes |
| Telegram | the event message (text + optional still) | follow-up only, or silence |
| Session | `openclaw message send` (healthcheck pattern) | isolated OpenClaw turn that **can** `message` the owner and **must not** steal the main chat |
| Person-detect | always | `wake: true` |

**One still per event.** Ping sends it. Wake prompt: the owner already
has the photo at `media=…`; do not send it again; do not fire siren;
you may read other entities and *ask*. If you have nothing to add, send
nothing (no “got it”).

The isolated turn is over before the owner types. “Yes, siren” is a
**later main-session** message, which may `ha.call_service` on that
account. Do not wait 60s inside the wake.

HA friendly names and states are untrusted data in the wake prompt
(inbound-content instinct). Wrap them; do not splice into instructions.

Default `wake: false`. Person watchers the owner asked to “wake me” set
true.

---

## 6. Agent-driven config

Self MCP (`scripts/mcp-homebrain.py`), not the HA MCP:

- `homebrain.watcher_list`
- `homebrain.watcher_set` — consent; summary is the full watcher
- `homebrain.watcher_delete` — consent

On set: account exists; `GET /api/states/<entity_id>` (and camera if
set) on that account **now**; refuse a miss. Do not let the model invent
ids. It should `ha.entity_search` first.

Target UX, two consents:

> when the front cam sees a person, ping me and turn on the porch light

1. `watcher_set` (`account=remote`, person sensor, camera, `wake` as
   asked)
2. `ha.automation_upsert` on that account (trigger that sensor `on`,
   action `light.turn_on` on the porch entity)

Stamp agent-made automations with alias prefix `[HomeBrain]` and a
stable `id` we generate.

Dashboard: optional later, read-only list of the same file.

---

## 7. HA automation tools (sibling; required for the sentence in §6)

Today the HA MCP can only *run* existing automations. Add:

- `ha.automation_list` / `ha.automation_get` — read, no consent
- `ha.automation_upsert` / `ha.automation_delete` — consent; summary is
  the JSON/YAML body. Delete requires id (and alias if present) to match.

UI-managed automations only, not packages. Same REST for scripts if we
want “when I say this, do that” as a script instead of a trigger.

Device-id automations and blueprints: out of scope.

---

## 8. Spikes (do these before writing the daemon)

Block wake (§9 P4) until both are answered on the box:

1. **Wake inject.** Heartbeat `isolatedSession` was built so hourly turns
   do *not* hit Telegram. Person-wake must message the owner without
   eating the main chat. Find the real CLI (`openclaw agent` / session
   spawn / target). If the only inject is the main session, ship
   ping-only.
2. **Photo without llama.** Does `openclaw message send` take media? If
   not, `sendPhoto` with the bot token and chat id healthcheck already
   resolves. Do not send the still through the model.

Also confirm HA websocket auth + `subscribe_events` against local HA and
a remote account with the stored LLAT (TLS on the latter).

---

## 9. Phasing

| Phase | Ship | Gate |
|---|---|---|
| **0** | Spikes in §8 | Wake CLI + photo send known |
| **1** | Websocket daemon + ping **text** + cooldown + cold-start seed | Flip a local `input_boolean` → Telegram. Then the same watcher on a remote account. |
| **2** | Optional `camera_entity_id` on ping | Remote still arrives; llama can be stopped |
| **3** | Self-MCP watcher list/set/delete | Sentence without the automation half |
| **4** | Wake, only if spike (1) is clean | Person watcher with `wake: true`; no duplicate still; no self-siren |
| **5** | `ha.automation_upsert` (+ get/list/delete) | Full sentence: ping + light |

P1–P3 are useful without wake and without upsert. Do not block ping on
the agent.

---

## 10. Tests

- Unit: transition `off→on` fires; `on→on` attrs do not; `unavailable`
  ignored; cooldown; cold-start seed does not fire; unknown account
  refused; `watcher_set` with missing entity refused; same id replaces.
- Camera fetch mocked; never assert `camera.snapshot`.
- Consent envelope on set/delete/upsert matches other self/HA act tools.
- Backup/restore includes `ha_watchers.json`; after restore, daemon seeds
  and stays quiet.
- E2E: local `input_boolean` on the GPU box; a remote account only after
  local is green. Hardware lock in `TESTING.md` as usual.

---

## 11. What to refuse in review

- Polling “because stdlib.” We chose trigger; local and remote are both
  websockets.
- A local-only webhook “because same box.” Loopback websocket is the HA
  bus.
- Siren/light fields on the watcher JSON.
- Wake as the only delivery path for the still.
- Watcher loop inside `mcp-homeassistant.py` or `mcp-homebrain.py`.
- Public inbound webhook / Pangolin on this box for a remote HA to POST.
- Vision / “is this really a person?”
- Dashboard editor before MCP CRUD.
- `exec` into `/config/automations.yaml`.
