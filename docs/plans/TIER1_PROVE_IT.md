# Tier 1 — "Prove It" — Design & Implementation Plan

**Status:** Planning (2026-08-02). Item 0 in progress.
**Date:** 2026-08-02
**Context:** Every serious defect found in the last three months shares one shape: **the box
asserts a success it never verified.** HA password rotation reported "rotated" for months while
writing nothing (#145). Off-site backup reported "synced" and had never once been restored.
`_decrypt()` returns the ciphertext when decryption fails, so a corrupted key surfaces as an
unexplained 401 three layers away. `TESTING.md` documented broken behaviour as expected output.

Tier 1 is not a feature list. It is the same fix applied five times: **make the box prove the
claim it is making, and fail loudly when it cannot.**

---

## 1. Goals & Non-Goals

### Goals
- **No silent fallbacks on a security-critical path.** If a secret cannot be decrypted, that is an
  error with a name, not an empty string or a passthrough.
- **Every recovery path has been walked end to end on real hardware** before it is claimed to work.
- **The user can ask the box to prove it** — one button, honest answers, including "no".
- **Notifications survive the thing they are notifying about.** A push channel that lives inside
  the box cannot report that the box is dying.

### Non-Goals
- No new services, no new containers, no new daemons. Every item below lands in code that already
  runs.
- No dashboard redesign. New UI is a card or a screen in the existing wizard, styled with the
  existing classes.
- No retry/queue/backoff frameworks. A failed notification logs and moves on.
- No abstraction over the two notification transports. There are two; an `if` is the right shape.

---

## 2. Order of work, and why

| # | Item | Why here |
|---|------|----------|
| 0 | `.env` parse truncation + loud decrypt | Foundation. Item 1 exercises `restore.sh`, which is where the bug lives. Fixing it after would mean re-running the hardware E2E. |
| 1 | Bare-metal restore in the setup wizard | The only remaining **total data loss** path. Highest severity, and it is the item that proves off-site backup was ever real. |
| 2 | System self-test | Cheapest way to stop the whole defect class recurring. Also the natural place to surface items 0 and 1's health. |
| 3 | Email fallback for notifications | Small, self-contained, and item 4 depends on the box being able to reach the user. |
| 4 | Dead-man's switch (registrar heartbeat) | Last, because its Worker half needs the user's Cloudflare account — I can build and test the device half, not deploy the other. |

---

## 3. Item 0 — `.env` parse truncation, and loud decryption

### The bug
`scripts/restore.sh:158` and `scripts/provision.sh:119` split `key=value` lines with
`while IFS='=' read -r key value`. Bash treats `=` as a field separator, so a **trailing** `=` is
consumed as a delimiter rather than kept as data. A Fernet key is base64 of 32 bytes and therefore
*always* ends in exactly one `=`. Every restore corrupts `HOMEBRAIN_EMAIL_KEY` from 44 to 43
characters, with 100% probability.

The block exists *specifically* to keep account tokens decryptable across a restore. It destroys
the key instead.

The damage is invisible because `Fernet(key)` raises, and all three MCP `_decrypt()` helpers catch
`Exception` and **return the input unchanged** — so the server hands `gAAAAA…` to Home Assistant as
a bearer token. The user sees "the agent's HA tools stopped working". Nothing anywhere says "key".

`provision.sh` has the same pattern over `NEWT_SECRET` / `FACTORY_PASSWORD` / `REGISTRAR_SECRET`
(base64-ish values that can end in `=`).

### The fix
Three parts, in order of importance:

1. **Stop breaking it.** `key="${line%%=*}"; value="${line#*=}"` in both scripts. Split on the
   *first* `=` only, keep everything after it verbatim.
2. **Heal what is already broken.** Boxes that have restored even once are carrying a 43-char key
   on disk right now (`.58` is one). Re-pad on read: `key += "=" * (-len(key) % 4)`. This is
   idempotent for correct keys, so it is safe to apply unconditionally.
3. **Fail loudly.** `_decrypt()` must not return the ciphertext. When the blob looks like a Fernet
   token (`gAAAAA` prefix) and decryption fails, log once with the key length and return `""` —
   an empty credential fails at the auth boundary with an honest error instead of being sent
   over the wire.

The three MCP servers each carry their own copy of `_decrypt`. Collapse them into `mcp_common.py`
— one implementation, one place to get the padding right. `src/integrations.py` keeps its own
(it cannot import from `scripts/`), but gets the same normalizer.

### Done when
- Unit test: a key with padding survives a round-trip through the restore merge.
- Unit test: a 43-char key still decrypts a token encrypted with the 44-char original.
- Unit test: `_decrypt` of a Fernet-shaped blob under a wrong key returns `""`, not the blob.
- On hardware: back up, wipe, restore, and confirm `HOMEBRAIN_EMAIL_KEY` in `.env` is 44 chars
  and that a stored account token decrypts.

---

## 4. Item 1 — Bare-metal restore in the setup wizard

### What exists already
`restore.sh --from-offsite` shipped in Phase 5d: argument parsing, `offsite_list`, `offsite_size`
pre-flight space check, `offsite_fetch`. The *mechanism* is there. What is missing is that a user
holding a dead box and a new one has no way to reach it — `restore.sh` is only callable from a
dashboard that requires a master password the new box does not have.

`restore.sh`'s tail also assumes a **running stack** (`docker compose exec nextcloud occ …`). So
the wizard cannot simply run it on bare metal; it must bootstrap `.env`, deploy, *then* restore.

### Shape
A third path on the first-boot wizard, next to "Set up new" — **"Restore from backup"**:
1. Ask for the off-site credentials (the same fields the backup settings card already collects)
   and the master password that encrypted the archive.
2. List what is there (`offsite_list`), show dates and sizes, let the user pick.
3. Write `.env` from the wizard answers, deploy the stack, then hand off to `restore.sh --from-offsite`.
4. Stream to the existing installer log view. It is the same UI as a fresh install.

Reuse, do not rebuild: the wizard already has a log-streaming screen, the settings page already
has an off-site credential form, `restore.sh` already knows how to fetch and unpack.

### Done when
- On hardware: take a real backup to an off-site target, `nuclear_reset.sh`, then bring the box
  back **through the wizard alone** — no SSH, no manual `.env`.
- Nextcloud files, HA config, and Vaultwarden all present afterwards.
- The restored box's `HOMEBRAIN_EMAIL_KEY` is intact (this is item 0's proof, too).

**E2E honesty note:** the test box's off-site target will be a local rclone remote on a path that
survives the wipe. That proves the *software* path — fetch, decrypt, unpack, redeploy. It does not
prove physical off-siteness or WAN transfer of a 78 GB archive.

---

## 5. Item 2 — System self-test

### Shape
One button on the dashboard: **"Run self-test"**. It does not check that a service is *running* —
the existing health checks already do that, and running is not the same as working. It checks that
each claim the box makes is **true right now**:

| Check | How it proves it |
|-------|------------------|
| Master password | Actually logs in to Nextcloud, HA, and the dashboard with the stored value |
| Backup | Reads the newest local archive's manifest and its age |
| Off-site | `offsite_list` — is the newest remote archive newer than `OFFSITE_KEEP_DAYS` allows to be missing |
| Instance secrets | `HOMEBRAIN_EMAIL_KEY` is 44 chars and decrypts one stored token |
| Reachability | The tunnel answers from outside, or says plainly that it does not |
| Updates | Current version vs. latest release |

Each row renders pass / fail / skipped-and-why. **"Skipped" is a real outcome** and must never be
rendered as a pass — that is the defect class this item exists to end.

The HA row is the direct descendant of #145: had this existed, the rotation bug would have been
caught the day it shipped.

### Done when
- On hardware: every row passes on a healthy box.
- On hardware: deliberately break one thing (wrong `HA_ADMIN_PASSWORD` in `.env`) and confirm the
  row goes red with a usable message.

---

## 6. Item 3 — Email fallback for notifications

Today's alerts go out over the agent's push channel, which lives on the box. A box that cannot boot
cannot tell you it cannot boot.

`mcp-email.py` already holds working SMTP credentials. Route notifications through SMTP when the
push channel is unavailable — same message, second transport, no queue. Log both outcomes.

### Done when
- On hardware: stop the push channel, trigger a backup failure, and receive the email.

---

## 7. Item 4 — Dead-man's switch

The box POSTs a heartbeat to the registrar Worker on a timer. If the Worker sees no heartbeat for
N hours, it emails the owner. This is the only alert that works when the box is off, and it is the
only Tier 1 item whose failure mode is "you find out in three weeks".

**Split delivery.** The device half (timer, POST, backoff, a settings toggle) I can build and prove.
The Worker half needs the user's Cloudflare account, a KV namespace, a cron trigger, and an email
provider — that is theirs to deploy. Plan accordingly: ship the device half behind a toggle that is
off until the endpoint exists, and hand over the Worker source plus deployment steps.

---

## 8. What this plan deliberately leaves out

- **Encrypted-at-rest disk.** Real, and much larger than Tier 1.
- **Multi-user Nextcloud provisioning from the dashboard.** Feature work, not a broken promise.
- **Anything that needs a second physical box.** Replication is already covered by the miami/berlin
  pair; adding a third topology proves nothing new.
