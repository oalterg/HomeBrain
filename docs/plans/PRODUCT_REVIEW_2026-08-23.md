# Product & engineering review — 2026-08-23

**Status:** open. New findings in §3 are unstarted.
**Predecessor:** [`PRODUCT_REVIEW_2026-08.md`](PRODUCT_REVIEW_2026-08.md) (2026-08-12),
still the action list. This does **not** supersede it. It audits it eleven days
and thirty-seven commits later, and adds what a fresh read of the tree turned up.
**Essence:** *Your private cloud and agent. Nothing leaves the box.*

---

## TL;DR

Thirty-seven commits landed since the last review. They are good commits —
HA watchers, backup unlock, the recovery sheet, Telegram attachments, the model
swap. Not one of them is on the list the last review wrote.

The two items that review called highest blast radius have not moved.
`docs/DISCLOSED_CREDENTIALS.md` still opens with *"Nothing here has been
rotated"* and was last touched on 2026-08-02. `docker-compose.yml` still
contains zero `cap_drop`, zero `no-new-privileges`, zero `mem_limit`, and was
last touched on 2026-07-25 — before the review that asked for them existed.

Meanwhile the god files grew. `utilities.sh` is up 513 lines in eleven days, a
20% increase in the file the review named as a source of the dual-source bug
class. The product got better. The trust posture did not, and the debt the
review priced got more expensive.

Five new findings below. The cheapest one (D1) makes `pytest scripts/tests/`
red on clean `main` right now; CI is green only because it hardcodes a file
order that happens to dodge the problem.

---

## 1. Status of the open list

Verified against the working tree on 2026-08-23.

| ID | Item | State |
|---|---|---|
| **A1** | Rotate disclosed credentials | **Not started.** Doc unchanged since 2026-08-02; `misc` still on disk at the repo root. |
| A2 | `AGENTS.md` behavioral only | Done. Still 1,729 bytes, still no repo internals. |
| A3 | No cloud dead-man's switch | Closed by decision. `registrar/heartbeat.js` undeployed. |
| A4 | Bare-metal off-site restore | Walked 2026-08-12, plus two more hardware walks since (#207–#210). |
| B1 | Inbound agent content | Distilled to [`INBOUND_AGENT_CONTENT.md`](INBOUND_AGENT_CONTENT.md). Still "no implementation plan yet." As designed. |
| B2 | Dashboard is HTTP on the LAN | **Partial.** The README stopgap is oblique — see D4. |
| **B3** | Container hardening | **Not started.** Compose file untouched since 2026-07-25. |
| B4 | `INTEGRATIONS_PLAN.md` historical | Done, and the header is exactly right. |
| **C1/C2** | Extract when touching; one owner per fact | **Not applied.** See the growth table and D5. |

### The growth table

Lines, 2026-08-12 → 2026-08-23:

| File | Then | Now | Δ |
|---|---|---|---|
| `scripts/utilities.sh` | 2,583 | 3,096 | **+513 (+20%)** |
| `src/app.py` | 5,244 | 5,566 | +322 |
| `scripts/common.sh` | 1,760 | 1,854 | +94 |
| `src/static/dashboard.js` | 3,064 | 3,143 | +79 |
| `src/integrations.py` | 1,645 | 1,658 | +13 |

C1 said: *do not rewrite `app.py`; extract the next slice when those files are
being touched anyway.* Those files were touched, a lot. Nothing was extracted.

The slice C1 named is still sitting there and is still clean: the backup /
restore / drives routes run roughly `app.py:1574–2560`, about a thousand
contiguous lines, with `find_backup` / `backup_search_dirs` as their only
shared helpers. It has not gotten harder to lift. It has gotten bigger.

---

## 2. What shipped instead — credit where it is due

This is not a list of wasted work.

**Backup unlock (#205, #206, #203)** closed a real product gap: the recovery
phrase now opens archives, so the owner who lost the master password is not
holding encrypted files they cannot read. That is the second half of "one
secret, one recovery," and without it the first half was a promise the backup
could not keep. It came with hardware walks (#207–#210) and a review doc that
says plainly what is still imprecise. This is the project at its best.

**HA watchers (#193, #195, #196)** put a Telegram ping in front of the house
without routing it through llama — the same instinct as `healthcheck.py`, and
the right one. Alerts that need the GPU up cannot tell you the GPU is down.

**#191** (heartbeat swallowing an owner's message) and **#187/#188** (turn
budgets) are the agent becoming reliable rather than impressive.

The problem is sequencing, not quality. A box with excellent backup unlock and
an unrotated recovery phrase is a box with excellent backup unlock and a live
takeover credential.

---

## 3. New findings

Verified on the working tree, 2026-08-23. Ordered by blast radius.

### D1. `pytest scripts/tests/` is red on clean `main`

Run the obvious command on a clean checkout and 15 tests fail:

```
15 failed, 332 passed, 4 skipped
```

Run CI's exact invocation and it is green — 290 passed. The difference is
argument order.

`scripts/tests/test_backup_discovery.py:33-36` rebinds module globals on the
imported `app` module and never restores them:

```python
hb.BACKUP_DIR = drive
hb.INTERNAL_BACKUP_DIR = internal
hb.get_env_config = lambda: {"BACKUP_MOUNTDIR": configured}
```

`app` is one entry in `sys.modules` for the whole session. After that file
runs, every later test sees a `get_env_config` that returns a dict with no
`MASTER_PASSWORD` — so `test_master_password.py` and `test_setup_credentials.py`
watch the code generate a fresh passphrase and assert against the one they
supplied. `BACKUP_DIR` and `INTERNAL_BACKUP_DIR` are left pointing at temp
directories that the `TemporaryDirectory` context already deleted.

Alphabetically, `test_backup_discovery` sorts before `test_master_password`.
In `ci.yml` the file list happens to put `test_master_password.py` third and
`test_backup_discovery.py` seventh. **CI is green because of the order someone
typed in a YAML file.** Reproduce the pollution in isolation:

```
pytest scripts/tests/test_backup_discovery.py scripts/tests/test_master_password.py
  -> 11 failed, 10 passed
```

Fix: the `monkeypatch` fixture, which restores on teardown. About five lines in
one file.

**Related, same root:** two test files are not referenced in `ci.yml` at all —
`test_mcp_nextcloud.py` (814 lines) and `test_mcp_email.py` (238 lines). That
is 1,052 lines of tests for the two MCP servers with the widest reach — the
file store and the inbox — that have never run in CI. The explicit-file-list
pattern means a new test file runs only if someone remembers the second half of
the job. That is the same silent-success shape the suite exists to catch, aimed
at the suite itself. Prefer `pytest scripts/tests/` once D1 is fixed.

### D2. A fresh box has no version, and the downgrade guard is blind on its first update

`version.json` is written in exactly one place: `update.sh:394`. Neither
`provision.sh` nor `deploy.sh` writes it. So on a box that has been installed
but never updated:

- `get_local_version()` returns `{"channel": "unknown", "ref": "unknown"}`
  (`app.py:689`) and that is what the dashboard has to show.
- `abort_if_downgrade()` reads the file, finds nothing, and passes empty
  strings into `detect_downgrade` (`update.sh:34-38`). The `[ -n "$inst_channel" ]`
  arm never fires. Only the Nextcloud image tag is still compared.

Now combine that with `install`, which fetches
`refs/heads/main.tar.gz` — unconditionally, with no checksum and no release
pin. Every new box therefore starts on unreleased `main`, which is ahead of
every stable tag by definition, and its first "Check for updates → Update" on
the stable channel walks the manager code **backwards** — the exact move the
guard exists to refuse — with no signal available to notice.

The Nextcloud half is safe (the compose tag is on disk and gets compared). The
manager half is not, and `common.sh:47-50` describes precisely what that costs.

Cheapest fix: have `provision.sh` write `version.json` with the channel and ref
it actually installed. That makes the guard honest on day one and gets
"unknown" off the dashboard.

### D3. The agent's audit trail reaches no one

This is the finding that grew when I pulled on it. It is three things: a
missing UI, a dead log viewer underneath it, and three structural limits that
should be decided deliberately rather than inherited.

**What is recorded.** `mcp_common.audit()` (`scripts/mcp_common.py:157`) writes
one JSON line — `ts`, `server`, `action`, plus per-call fields — for 32 distinct
actions across the five servers:

| Server | Audited actions |
|---|---|
| `email` | `fetch`, `send`, `send.fail`, `draft`, `archive`, `flag`, `attachment`, `attachment.too_large`, `imap_error` |
| `homeassistant` | `call_service.ok/.fail`, `call_service_raw.ok/.fail`, `automation_upsert.ok/.fail`, `automation_delete.ok`, `camera_image.ok`, `camera_image.too_large` |
| `nextcloud` | `upload`, `download`, `download.too_large`, `share`, `notes.create`, `notes.update` |
| `vault` | `search`, `reveal`, `create_login`, `update_login` |
| `homebrain` | `backup_now`, `service_restart`, `watcher_set`, `watcher_delete` |

That is a good list. It covers the Act and Reveal tiers, and it captures the
failures as well as the successes — `send.fail` matters more than `send`.

**Where it goes: two copies, and neither reaches the owner.**

*Copy one, the file.* `/var/log/homebrain/mcp-<server>-audit.log`. `LOG_FILES`
(`app.py:279-287`) has seven entries and none is an audit log, so the Logs tab
cannot select one. Neither `app.py`, `dashboard.js`, nor `dashboard.html`
mentions the files. Reading them requires a shell.

*Copy two, stderr.* `audit()` writes to `sys.stderr` **before** the file. The
MCP servers are stdio subprocesses of the OpenClaw gateway, so that copy lands
in the gateway's journal — which the Logs tab does offer, as "OpenClaw agent."

Except that option is broken. `app.py:300` declares
`JOURNAL_SERVICES = {"openclaw", "llama-server"}` and `app.py:1521-1523` runs:

```
journalctl -u openclaw -n 200 --no-pager --output=short-iso
```

**There is no unit named `openclaw` anywhere in this repo.** The gateway is a
*user-level* unit called `openclaw-gateway`, installed under
`~/.config/systemd/user/` (`utilities.sh:1478`, `:2300-2302`). A root
`journalctl -u openclaw` matches no system unit and returns nothing. The
project already knows the correct incantation and prints it in an error path —
`utilities.sh:2343`:

```
sudo -u ${HOMEBRAIN_USER} journalctl --user -u openclaw-gateway -n 50
```

So this is the dual-source bug class again, with the unit name as the fact:
right in `utilities.sh`, wrong in `app.py`. `llama-server` is unaffected — it
*is* a system unit (`/etc/systemd/system/llama-server.service`,
`utilities.sh:886`), so that option works, which is exactly why the broken one
is easy to miss.

The consequence is bigger than the audit trail. **The agent — the product's
differentiator, the thing running as root-equivalent — has no working log
surface in the dashboard at all.** Every "why did it do that" question needs
SSH today.

Note the fix is not a one-word rename: root cannot read another user's journal
with `--user`. It needs the user-journal form, e.g. matching
`_SYSTEMD_USER_UNIT=openclaw-gateway.service` with the homebrain `_UID`, or
shelling through `sudo -u ${HOMEBRAIN_USER}`.

**Three limits to decide on, not bugs to fix.**

1. **Audit writes fail open, silently.** `mcp_common.py:167-173` wraps the file
   write in `except OSError: pass`. If the directory is unwritable or the disk
   is full, the action still proceeds and no record is kept — and disk-full is
   precisely when you want the record. Compare `selftest.py`'s three-outcome
   rule: `skip` must not render as `pass`. An audit that cannot write should
   say so somewhere.
2. **History is about five weeks.** `logrotate-homebrain` is `weekly` +
   `rotate 4`. Fine for debugging, short for "when did this credential first
   get revealed."
3. **The agent can rewrite its own audit log.** `/var/log/homebrain` is
   `chgrp homebrain` + `chmod 2775` (`provision.sh:295-297`) so the stdio MCP
   subprocesses can append; the logs are homebrain-owned `0640`. The agent runs
   as `homebrain` with NOPASSWD sudo. This is a debugging aid, not
   tamper-evidence, and that is *consistent* with §B1's refusal to pretend the
   agent is sandboxed — it just needs writing down next to the feature so
   nobody later mistakes it for a forensic control.

**What is not covered at all.** `tools.exec`. The typed tool surface is
audited; the unbounded surface that motivated §B1 is not, and it is the one
that can `docker run -v /:/host`. Also unaudited, on the `homebrain` server
only: `logs_tail`, `service_status`, `gpu_stats`, `version`,
`integrations_status` — the agent reading the box rather than changing it. The
read/act split is defensible; `logs_tail` is the one I would reconsider, since
it is how an injected instruction would go looking for secrets in logs.

**Order to fix.** (1) The unit name — that is a live bug with a blast radius
well beyond audit. (2) Two `LOG_FILES` entries and two `<option>` tags for the
audit logs. (3) The three limits, written down wherever they land.

### D4. The LAN HTTPS edge exists, and humans are not sent to it

Port bindings in `docker-compose.yml`:

| Service | Binding | |
|---|---|---|
| redis | `127.0.0.1:6379` | ✅ |
| vaultwarden | `127.0.0.1:8082` | ✅ |
| proton-bridge | `127.0.0.1:12143/12025` | ✅ |
| caddy | `8443` / `8444` | HTTPS, `tls internal` |
| **nextcloud** | **`8080:80`** | plain HTTP, all interfaces |
| **homeassistant** | **`8123:8123`** | plain HTTP, all interfaces |
| **manager** | **`0.0.0.0:80`**, gunicorn as root | plain HTTP |

Caddy was built for Bitwarden clients that refuse plain HTTP. It works. But
8080 and 8123 stay open beside it, and the product routes people to the
cleartext ones:

- `app.py:4053` — in local mode the "Documents (Encrypted)" link is
  `http://<lan-ip>:8080/…`, while 8444 serves the same Nextcloud over TLS.
- `dashboard.js:804` — the dashboard tells the owner to mint a Home Assistant
  long-lived token at `http://<host>:8123/profile/security`. Home Assistant has
  no HTTPS edge at all. `DISCLOSED_CREDENTIALS.md` rates that token as *"every
  device HA controls — locks, cameras, alarms"* and notes it does not expire.
  It is created, displayed, and copied over cleartext on the LAN.

B2's stopgap was supposed to be a README sentence saying this plainly. What the
README says (line 32) is *"On the LAN, open `http://<server-ip>`. Nextcloud and
Vault also speak HTTPS on local ports."* That tells a careful reader the
dashboard is HTTP by omission, says nothing about Home Assistant, and does not
mention that the master password crosses the LAN in the clear — the password
that is also Nextcloud, HA, Vault, and the backup key.

Two things are separable here. Pointing the dashboard's own links at 8444
instead of 8080 is a small change with no bootstrap problem. Deciding whether
8080/8123 should be `127.0.0.1`-bound is a real decision with real breakage
(the HA companion app, existing bookmarks) and belongs with B2, not ahead of it.

### D5. `restore.sh` still hand-rolls what `common.sh` owns

C2 said: *before adding a branch to `restore.sh` that already exists in
`backup.sh`, put the helper in `common.sh`.* The helper is already there.

`common.sh:1332` defines `reconcile_nextcloud()`. It polls `occ status` up to
30 times over 60 seconds before deciding anything, with a comment explaining
why: *"occ is unavailable until the entrypoint finishes copying code on a fresh
image."* Its only caller is `update.sh:391`.

`restore.sh:578-587` runs the same sequence inline — `occ upgrade`,
`maintenance:repair`, `db:add-missing-indices`, `files:scan` — without the
readiness poll. Restore waits for container *health* (`restore.sh:536`), which
is not the same thing, and a restore is the fresh-image case that poll was
written for.

Worse, every step swallows its exit code:

```bash
docker compose … php occ upgrade || log_warn "Upgrade failed—check Nextcloud logs."
…
log_info "=== Restore Complete From: $BACKUP_FILE ==="
```

`=== Restore Complete ===` prints whether or not the schema migrated. The
dashboard's restore flow keys off that marker. A restore can report success
over a Nextcloud stranded on its "update needed" page — the owner gets a green
result and a broken files app, and the log line that would explain it is one of
four `log_warn`s in a scrolled-past console.

This is the exact lie class the project has spent months removing. Call
`reconcile_nextcloud` and decide deliberately which of those four steps is
allowed to fail quietly.

### D6. `NUCLEAR_RESET.md` says a shipped feature is awaiting approval

Its header reads *"Design Complete — Awaiting Implementation Approval."*
`scripts/nuclear_reset.sh` is 9,649 bytes, `/api/system/nuclear-reset` is
routed, and the wizard-restore path depends on it. B4's own reasoning applies:
stale plan docs are bad prompts, and this one invites an agent to implement a
feature that exists.

Five other plan docs carry no status line at all (`MEMORY_MCP.md`,
`PHOTOS_HARDENING.md`, `RECOVERY_PHRASE.md`, `RECOVERY_SHEET.md`,
`VAULT_PLAN.md`). The other fourteen have them — this is a small gap in an
otherwise well-kept set.

---

## 4. What I would do, in order

Nothing here is a feature. Guardrails unchanged: no frontend framework, no
Prometheus, no `app.py` rewrite.

**First, because it is an afternoon and it unblocks trusting everything else**

1. **D1** — `monkeypatch` in `test_backup_discovery.py`; switch `ci.yml` to
   `pytest scripts/tests/` so the two orphaned MCP test files start running.
   Expect them to surface something; that is the point.
2. **D3a** — fix the journal unit name so the Logs tab can read the agent at
   all. Today "OpenClaw agent" returns nothing, and every question about the
   product's differentiating feature needs SSH.
3. **D2** — `provision.sh` writes `version.json`. One heredoc.
4. **D6** — fix the `NUCLEAR_RESET.md` header.

**Then, the list that did not move**

5. **A1.** Rotate. It is ops, not code, which is exactly why it keeps losing to
   commits. It has now lost for three weeks. Follow the doc's order; the
   recovery phrase first, because it resets everything downstream of it.
6. **B3.** `cap_drop: [ALL]`, `no-new-privileges`, `mem_limit` — `newt`,
   `redis`, `caddy` first, as the review already scoped. While in that file,
   `homebrain-manager.service` has `User=root` and no systemd hardening
   directives at all; it holds the master password and deserves at least
   `ProtectHome`/`PrivateTmp` consideration in the same pass.

**Then, when the code is open anyway**

7. **D5** — call `reconcile_nextcloud` from `restore.sh`, and decide which
   post-restore steps may fail quietly. This is C2, and it is the third time
   the backup/restore pair has drifted.
8. **D3b** — audit logs in the Logs tab, and write down the three limits
   (fail-open writes, five-week history, agent-writable) next to the feature.
9. **D4** — point the dashboard's own links at 8444. Leave the binding decision
   with B2.
10. **C1** — the backup/restore/drives slice is still ~1,000 clean lines. D5
   opens that file. Take it then.

---

## 5. Still unusually good

So the review is fair, and so these do not get refactored away by someone who
only read §3.

- **`custom_401` (`app.py:5271`)** renders a complete login page from a
  `render_template_string`, self-contained *"on purpose — this 401 surface must
  work even if every template file is broken."* That is appliance thinking.
- **No `alert()` anywhere in 3,143 lines of `dashboard.js`.** One `confirm()`.
  Four responsive breakpoints. No framework.
- **Zero `TODO`/`FIXME`/`XXX`** in `src/` and `scripts/`. On a 332-commit
  codebase that is not an accident.
- **`logrotate-homebrain`** covers the audit logs, and its comment explains the
  `su root homebrain` line rather than leaving it as a mystery.
- **`BACKUP_UNLOCK_REVIEW.md`** exists because the team reviewed its own
  shipped feature and found a product gap in it. That habit is worth more than
  any finding in this document.
- **Thirteen of nineteen plan docs carry an accurate status line.** Most repos
  have none.

---

## 6. Bottom line

The appliance thesis is intact and the last eleven days made it more true: the
recovery phrase now opens the backups, which is the difference between a
recovery story and a recovery slogan.

What changed is the ratio. The last review produced a short, sharp list and
then thirty-seven commits went somewhere else. The two items on it that cannot
be fixed by shipping — a credential rotation and eight lines of YAML — are the
two that did not get done, and the file the review named as the debt centre
grew a fifth.

None of the new findings is architectural. D1 is five lines, D2 is a heredoc,
D6 is a sentence. D5 is deleting an inline copy in favour of a function that
already exists. D3 is a wrong string — `openclaw` where the rest of the repo
correctly says `openclaw-gateway` — plus two `<option>` tags, in service of a
security control the product already pays for and currently shows no one.

The uncomfortable one is the pattern rather than any single item: this codebase
is unusually good at finding its own lies and unusually reluctant to stop
shipping long enough to fix the boring ones. `pytest scripts/tests/` being red
on `main` while CI is green is that pattern in miniature — a real signal,
correctly generated, that nobody is positioned to see.
