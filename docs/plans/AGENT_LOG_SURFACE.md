# Agent log surface — make the agent readable from the dashboard

**Status:** implemented and walked on `.58` 2026-08-23 (branch
`feat/agent-log-surface`, unmerged). Results and the two defects the run found
are in §7.
**Parent:** [`PRODUCT_REVIEW_2026-08-23.md`](PRODUCT_REVIEW_2026-08-23.md) §D3.
**Depends on:** D1 (`pytest scripts/tests/` order pollution). See §4 — the new
test does not run in CI until D1 is fixed.
**Essence:** the owner never needs a shell. Today, every question about the
agent needs one.

---

## 1. What is actually broken

Two things, one of which is a live bug.

**The OpenClaw log option returns nothing.** `app.py:1521` runs
`journalctl -u openclaw`. There is no unit by that name. The gateway is a
*user* unit, `openclaw-gateway`, installed under
`~/.config/systemd/user/` (`utilities.sh:1478`). The correct incantation is
already written down in this repo — `utilities.sh:2343` prints it in an error
path. One fact, two owners, and the wrong one is the one the dashboard uses.

**The MCP audit logs have no surface at all.** Five files under
`/var/log/homebrain/`, 32 audited actions, `logrotate` already configured —
and no `LOG_FILES` entry, no `<option>`, no route.

Net effect: the box's differentiating feature is the one component you cannot
observe without SSH.

---

## 2. Shape of the fix

The Logs tab already does everything needed: a source selector, a fetch loop, a
level filter, a console. The whole job is to give it two more sources and
correct one command. **No new tab, no new endpoint, no JavaScript.**

Three decisions worth stating, because each one removes work:

**One merged audit view, not five.** The owner asks "what did the agent do,"
not "what did the Nextcloud MCP server do." Five options would be five times
the UI for the same question.

**Format server-side into the existing line grammar.** `renderLogs()`
(`dashboard.js:2640-2658`) classifies a line by looking for `ERROR` / `WARN` /
`INFO` in it, and anything unmatched falls to *Debug / raw* — which is
**unchecked by default**. Dumping raw JSON would give the owner an empty
console and no clue why. Emitting `<ts> INFO email.send …` instead means the
lines flow through the existing filter, colouring, and escaping with zero
frontend changes, and "show me only what failed" works for free.

**Fix the unit name in one place.** A small helper that maps a log target to a
`journalctl` selector, so the user-unit knowledge lives next to the only code
that needs it.

### Deliberately not doing

- A dedicated audit tab, table view, or JSON viewer.
- A new API endpoint. `/api/logs/<target>` already exists.
- ~~Any change to `dashboard.js`.~~ **Wrong** — the hardware run forced one.
  See §7.2.
- Log shipping, signing, hash-chaining, or an append-only sink.
- Auditing `tools.exec`. That is §B1 territory and a product decision.
- Widening retention beyond a token change (§3, P3).

---

## 3. The work

Three parts. P1 is a bug fix and could ship alone today; P2 is what makes P1
useful; P3 is mostly writing things down.

### P1 — Read the right journal (~8 lines, `src/app.py`)

`JOURNAL_SERVICES` stays as it is. Add one helper next to it:

```python
def journal_match(target):
    """journalctl selector for a log target.

    openclaw-gateway is a *user* unit (utilities.sh:1478), so `-u openclaw`
    matches nothing and returns an empty log. Root cannot use `--user` for
    another account either — it selects a user unit by unit name + uid.
    """
    if target == "openclaw":
        import pwd          # function-local, matching app.py:3927
        return ["_SYSTEMD_USER_UNIT=openclaw-gateway.service",
                f"_UID={pwd.getpwnam('homebrain').pw_uid}"]
    return ["-u", target]
```

and use it in `get_logs` (`app.py:1521`):

```python
cmd = ["journalctl", *journal_match(log_target),
       "-n", "200", "--no-pager", "--output=short-iso"]
```

The dropdown value stays `openclaw`, so `dashboard.html` needs no change here.
An unknown `homebrain` user raises inside the existing `try`, which returns a
visible `[Error reading journal …]` rather than a blank pane — the failure
mode we want.

### P2 — One merged audit source (~28 lines, `src/app.py` + 1 line of HTML)

```python
AUDIT_SERVERS = ("homeassistant", "nextcloud", "vault", "email", "homebrain")

def read_agent_audit(limit=200):
    """Merged, human-readable view of the five MCP audit logs.

    Each server appends JSON lines (mcp_common.audit). The Logs tab classifies
    a line by the level token in it, and unmatched lines land under a filter
    that is off by default — so emit a level, or the console renders empty.
    """
    recs = []
    for name in AUDIT_SERVERS:
        try:
            with open(os.path.join(LOG_DIR, f"mcp-{name}-audit.log")) as f:
                lines = f.readlines()[-limit:]
        except OSError:
            continue                       # server never ran; not an error
        for line in lines:
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass                       # a torn line must not blank the view
    if not recs:
        return "No agent activity recorded yet."
    recs.sort(key=lambda r: r.get("ts", ""))     # ISO-8601 UTC sorts lexically
    out = []
    for r in recs[-limit:]:
        action = r.get("action", "?")
        level = ("ERROR" if action.endswith(".fail") or "error" in action
                 else "WARN" if "too_large" in action else "INFO")
        extra = " ".join(f"{k}={v}" for k, v in r.items()
                         if k not in ("ts", "server", "action"))
        out.append(f"{r.get('ts','')} {level} "
                   f"{r.get('server','?')}.{action} {extra}".rstrip())
    return "\n".join(out)
```

Wire it in `get_logs`, ahead of the `LOG_FILES` branch:

```python
if log_target == "agent-audit":
    return read_agent_audit()
```

and one line in `dashboard.html:1038`:

```html
<option value="agent-audit">Agent activity (audit)</option>
```

The hyphen in `agent-audit` means it can never reach the
`docker compose logs` fallback, which is gated on `isalnum()`.

The empty case returns a sentence, not `""` — an empty console is
indistinguishable from a failed fetch, and that distinction is the house rule.

### P3 — The three limits

Two are decisions, one is three lines.

**Fail-open audit writes — fix, cheaply.** `mcp_common.py:167-173` swallows
`OSError` on the file write. `audit()` writes to stderr *first*, so the record
still reaches the gateway journal — but the new UI reads the *file*, so a
silent file-write failure now means a silently incomplete audit view. Make the
failure say so:

```python
    except OSError as e:
        sys.stderr.write(json.dumps({
            "ts": rec["ts"], "server": server,
            "action": "audit.write_failed", "error": str(e)}) + "\n")
```

After P1 that marker is visible in the OpenClaw log. Three lines, and it closes
the loop.

**Retention (~5 weeks) — leave it.** `logrotate-homebrain` is `weekly` +
`rotate 4`. Longer history is a one-token change (`rotate 12`) if a real need
appears. Do not pre-emptively grow logs on a box that also stores the owner's
photos.

**The agent can rewrite its own audit log — document, do not fix.** The dir is
`chgrp homebrain` + `chmod 2775` so the stdio subprocesses can append; the
agent runs as `homebrain` with NOPASSWD sudo. Making this tamper-evident means
a separate uid and an append-only sink, and the agent has root anyway — §B1 is
explicit that we do not pretend it is sandboxed. Add a comment above `audit()`
saying it is a debugging aid, not a forensic control, so nobody later builds a
security claim on it.

### Open question, one line either way

`homebrain.logs_tail` is unaudited. It is how a prompt-injected instruction
would go looking for secrets in logs. Adding `audit("homebrain", "logs_tail",
target=...)` is one line. Reads are otherwise unaudited by design, so this is a
deliberate exception or it is not — decide, do not drift into it.

---

## 4. Tests

**Sequencing:** a new file in `scripts/tests/` is invisible to CI until D1's
fix switches `ci.yml` to `pytest scripts/tests/`. Land D1 first, or add the
file to the explicit list in the same PR and note it as debt.

`scripts/tests/test_agent_audit.py` (new, ~60 lines), against a temp `LOG_DIR`:

- Records from several servers merge in `ts` order.
- `.fail` → `ERROR`, `too_large` → `WARN`, everything else → `INFO` — the
  assertion that keeps the console non-empty under default filters.
- A torn / non-JSON line is skipped and the rest still render.
- A missing server file is skipped without error.
- No records anywhere → the sentinel sentence, not `""`.
- `journal_match("openclaw")` returns the user-unit selector;
  `journal_match("llama-server")` returns `["-u", "llama-server"]`.

`audit.write_failed` is unit-tested here (unwritable dir under `tmp_path`) and
deliberately **not** exercised on hardware — see §5.

---

## 5. Hardware E2E — `.58`

`.58` is the only viable target: this is agent-observability work, and the RPi4
test box has no GPU, therefore no agent, therefore no audit records. So this
runs against **production**, which sets the constraints: read-first, no
provision, no reset, no destructive step, and a rollback that is one command.

`.58` is **not on 24/7**. Confirm it is up before planning around it.

### 5.1 Pre-flight

```bash
HB=homebrain@192.168.178.58
ssh $HB 'uptime; uname -m; ls /dev/dri/renderD* 2>/dev/null'
```

Take the lock (TESTING.md §Pre-flight; the path there is `/home/admin/...`,
but `.58`'s user is `homebrain`):

```bash
LOCK=/home/homebrain/using_test_target
ssh $HB "cat $LOCK 2>/dev/null"                    # who holds it
ssh $HB "pgrep -af 'scripts/(deploy|provision|nuclear_reset|restore|update)\.sh'"
ssh $HB "echo 'holder: <session> (D3 agent log surface)' > $LOCK"
```

### 5.2 T0 — prove the bug before writing code

This is the step that makes the rest worth doing. All read-only.

```bash
# T0a  the command the dashboard runs today — expect NO output
ssh $HB 'journalctl -u openclaw -n 5 --no-pager; echo "rc=$?"'

# T0b  the unit that actually exists — expect lines
ssh $HB 'systemctl --user is-active openclaw-gateway; \
         journalctl --user -u openclaw-gateway -n 5 --no-pager'

# T0c  the exact selector P1 will use, as root — expect the same lines
ssh $HB "echo admin | sudo -S -p '' journalctl \
         _SYSTEMD_USER_UNIT=openclaw-gateway.service _UID=\$(id -u homebrain) \
         -n 5 --no-pager"
```

If T0c is empty while T0b has lines, P1's approach is wrong and the plan stops
here.

```bash
# T0d  is the journal persistent? VOLATILE means gateway logs die on reboot
ssh $HB 'ls -d /var/log/journal 2>/dev/null || echo VOLATILE'

# T0e  do the audit files exist, and has audit ever worked on this box?
ssh $HB 'ls -l /var/log/homebrain/mcp-*-audit.log; \
         wc -l /var/log/homebrain/mcp-*-audit.log; \
         tail -n2 /var/log/homebrain/mcp-homeassistant-audit.log'
```

T0e is worth reading carefully. `.58` has been agent-active for months; if
those files are missing or empty, the fail-open path in P3 is already firing
and that is a finding in its own right.

### 5.3 Deploy

**Preferred: merge, then update through the dashboard.** The change is
log-viewing only — no writes, no state, revertible by re-running the update —
and hand-rsync is where this project's deploy incidents have come from.

```bash
MP='<MANAGER_PASSWORD>'
curl -sc /tmp/hb.jar -d "password=$MP" http://192.168.178.58/login
curl -sb /tmp/hb.jar -X POST http://192.168.178.58/api/manager/update \
     -H 'Content-Type: application/json' \
     -d '{"channel":"dev","target_ref":"main"}'
```

`.58` last matched `main` at `5f5493c` (#203), so this also lands **#205–#211**
(backup unlock, Qwen3.8 context, recovery reset). Expected on the beta channel,
but know it before you press it, and re-check `version.json` afterwards.

**Fallback, if pre-merge proof is wanted:** hand-deploy `src/app.py` and
`src/templates/dashboard.html` — and follow the trap protocol first, because
`app.py` is exactly the file that carries other sessions' WIP:

```bash
git status --short && git diff src/app.py | grep '^@@'
```

If the tree is mixed, build a clean copy from `git show main:<path>` plus your
own hunks. Rollback copies to `/opt/homebrain-rollback-agentlog/`, then
`systemctl restart homebrain-manager`.

### 5.4 T1–T6 — verify

```bash
# T1  D3a: the OpenClaw source returns content (was empty)
curl -sb /tmp/hb.jar http://192.168.178.58/api/logs/openclaw | head -20

# T2  D3b: merged audit, ascending ts, more than one server represented
curl -sb /tmp/hb.jar http://192.168.178.58/api/logs/agent-audit | tail -20
```

**T3 — the default-filter trap. Browser, not curl.** Dashboard → Logs →
*Agent activity (audit)* with the filters left as they ship (Errors, Warnings,
Info checked; Debug **un**checked). Lines must be visible. This is the entire
reason P2 emits a level token, and curl cannot prove it.

**T4 — failure filtering.** Uncheck Info and Warnings. Only `.fail` /
`*_error` lines remain. If the box has never recorded one, record that as
untested rather than manufacturing a failure on production.

**T5 — fresh capture.** One audited action over Telegram, read-only and
reversible: ask the agent to send you a small existing file from Nextcloud
(`nextcloud.download` is audited). Fallback if that path is awkward: "search my
vault for &lt;term&gt;" (`vault.search`, audited — needs the Vault MCP
unlocked). Then:

```bash
curl -sb /tmp/hb.jar http://192.168.178.58/api/logs/agent-audit | tail -3
```

The new record must carry a `ts` within the last minute.

**T6 — regression.** `llama-server` still returns content; `manager` and
`backup` file sources are unchanged; the container sources still work.

**T7 — not run on hardware.** `audit.write_failed` needs an unwritable
`/var/log/homebrain`. Breaking permissions on a production log directory to
observe a log line is a bad trade; it is unit-tested instead (§4).

### 5.5 Cleanup

```bash
ssh $HB "rm -f /home/homebrain/using_test_target"
```

Release the lock on failure too. If the fallback deploy was used, either leave
the merged state or restore `/opt/homebrain-rollback-agentlog/`.

### 5.6 Honesty notes

Written in the same spirit as the review's §A4 — what this run does *not*
prove:

- The merged view is capped at 200 records. A busy day is not fully
  represented. It is a window, not "the audit log," and should not be described
  as one.
- Retention is unchanged at ~5 weeks.
- The agent can still rewrite these files. The tab shows what is *in* them, not
  what happened.
- If T0d reported `VOLATILE`, T1's content does not survive a reboot, and the
  audit files are the only durable copy.
- A pass here is a pass on x86 + GPU. There is no no-GPU variant of this
  surface to test, because there is no agent.

---

## 6. Sign-off

- [ ] T0a empty, T0b and T0c non-empty — the bug is proven, and so is the fix's
      selector
- [ ] T0e recorded (audit files present, non-empty, or the finding written down)
- [ ] Unit tests green via `pytest scripts/tests/` (D1 fixed, or file added to
      `ci.yml`)
- [ ] T1 returns gateway journal content
- [ ] T2 returns merged, ts-ordered, multi-server lines
- [ ] **T3 visible in the browser under default filters**
- [ ] T4 filters to failures, or recorded as untested
- [ ] T5 fresh record appears within a minute
- [ ] T6 no regression on the other log sources
- [ ] Lock released; `version.json` re-checked
- [ ] §5.6 honesty notes updated with what actually happened


---

## 7. Results — walked on `.58`, 2026-08-23

x86_64 + Radeon RX 9060 XT, lock held for the write half, released after.

### 7.1 T0 — the bug, proven before any code was written

| Step | Expected | Actual |
|---|---|---|
| T0a `journalctl -u openclaw` | nothing | `-- No entries --`, **rc=0** |
| T0b `--user -u openclaw-gateway` | lines | `active`, live gateway journal |
| T0c root `_SYSTEMD_USER_UNIT=…` + `_UID` | same lines | same lines, `short-iso` clean |
| T0d journal storage | — | **PERSISTENT** (`/var/log/journal`) |
| T0e audit files | present? | all five, **231 records**, `homebrain:homebrain 0640` |

T0a's `rc=0` is the whole bug in one line: journalctl reports *success* while
printing nothing, so the pane went blank with no error to notice.

T0e cleared a suspicion the plan raised: audit has been working on this box all
along. The fail-open path in P3 has never fired here. Only the surface was
missing.

Also checked before surfacing anything: `vault.reveal` records
`item_id`/`item_name`/`reason`, and `email.send` records `to`/`subject`. No
secret values reach these files, so rendering them in the dashboard is safe.

### 7.2 Two defects the run found that the plan did not predict

**D3-a: a multi-line field value split one record across three console lines.**
Home Assistant records the upstream body on a failed call, and a 500 page
carries newlines:

```
{"action": "call_service_raw.fail", …,
 "resp": "500 Internal Server Error\n\nServer got itself in trouble"}
```

`json.loads` turns those back into real newlines, and the formatter
interpolated them straight through. Six real records on `.58` rendered as 18
lines, and the 12 continuation lines carried no level token — so the default
filters hid them while the parent line showed. Caught by reading the T2 output
rather than trusting the line count. Fixed by collapsing whitespace in values;
regression test written from the real record before the fix.

**D3-b: fixing the fetch was not enough — the console was still blank.**
The browser run (T6) showed `openclaw` returning 200 lines and rendering
**zero**. `renderLogs()` classifies by finding `ERROR`/`WARN`/`INFO` in a line;
journalctl's `short-iso` output plus OpenClaw's bracketed tags contain none, so
every line fell under *Debug / raw*, which ships unchecked.

Measured on `.58`:

| Source | lines returned | visible under default filters |
|---|---|---|
| `openclaw` | 200 | **0** |
| `llama-server` | 200 | **0** |
| `nextcloud` | 100 | **0** |
| `db` | 78 | 16 |
| `manager` | 100 | 100 |

Two of those were **already broken before this change** — selecting Nextcloud
or llama-server on production showed an empty console and always had. Nobody
noticed, because an empty log pane reads as "quiet", which is the same lie
`selftest.py` exists to prevent.

So the plan's "no JavaScript changes" was wrong. `renderLogs` now says which,
when a source returned lines and every one was filtered out:

> `200 lines hidden by the filters above — tick "Debug / raw" to see them.`

Five lines. It fixes the blank console for every source, not just the agent's.
The alternative — treating unclassified lines as INFO so they show by default —
was rejected: the filter is genuinely useful on `manager`, where the raw class
is stack-trace continuation lines. **Open to revisit** if reading the agent's
log behind a checkbox proves annoying in daily use.

### 7.3 T1–T6

| Test | Result |
|---|---|
| T1 OpenClaw source returns content | **PASS** — 200 lines / 51,718 bytes (was `-- No entries --`) |
| T2 merged audit, ordered, all servers | **PASS** — 199 lines, May 25 → Aug 23; 141 HA / 20 vault / 18 homebrain / 11 NC / 10 email; 0 lines without a level |
| T3 visible under **untouched** default filters | **PASS** — 200 lines, all audit-shaped; 187 `log-info`, 12 `log-error`, 1 `log-warning` |
| T4 filter to failures | **PASS** — 12 ERROR lines, no leakage: 7 camera 500s, 3 light `call_service.fail`, 2 `email.imap_error` |
| T5 fresh capture, real agent turn | **PASS** — `openclaw agent` (no `--deliver`) → camera snapshot → record in the dashboard **79 s old** |
| T6 regression | **PASS** — `manager` 100 visible; `openclaw`/`llama-server`/`nextcloud` now show the hint instead of a blank pane |
| T7 `audit.write_failed` | **not run on hardware**, by design — unit-tested instead |

T3, T4 and T6 ran in a real browser (Playwright/Chromium). T6 is the reason:
curl reported T1 as a pass while the user-visible console was still empty.

Local gates before deploy: **311 passed, 4 skipped**; ruff `E9,F` clean;
`compileall` clean; `shell=True` gate clean.

### 7.4 Honesty notes

- The merged view is capped at 200 records. `.58` currently holds 232, so the
  view already elides the oldest. It is a window, not "the audit log."
- Retention is unchanged at ~5 weeks (`weekly` + `rotate 4`).
- The agent can still rewrite these files. The tab shows what is *in* them.
- `audit.write_failed` has never fired on `.58` and was not provoked there.
- Reading the agent's own journal still needs one click on *Debug / raw*. The
  hint makes that discoverable; it does not make it automatic.
- x86 + GPU only. There is no no-GPU variant of this surface, because there is
  no agent.
- `.58` runs this branch's four files unmerged. Pristine `origin/main` copies
  are at **`/opt/homebrain-rollback-agentlog/`**, path-mirrored, so a rollback
  is one `rsync`. A dashboard update from `main` will overwrite them, which is
  the intended way to re-sync once this merges.
