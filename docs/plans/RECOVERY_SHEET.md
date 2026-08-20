# HomeBrain Recovery Sheet — Audit & Consolidation Plan

The downloadable `.txt` credential sheet: what shipped, what is wrong with it, and
the minimum change that fixes it. Extends `docs/plans/RECOVERY_PHRASE.md` §9
("Follow-up (2026-07-29)"), which introduced the download in one paragraph and
never specified it.

---

## 1. What exists today

PR #144 (`6fcee05`, v2026.07.29) added a client-side `.txt` download of whatever
secrets are on screen. It landed as **two independent implementations**:

| Surface | Implementation | Contents |
|---|---|---|
| Setup handover | `src/templates/installing.html:195` (inline script), button at `:112` | master password **and** recovery phrase |
| Settings -> Recovery Phrase reveal | `src/static/dashboard.js:2722` (`downloadCredsSheet`) + `:2761` (`downloadRevealedPhrase`), button at `dashboard.html:975` | phrase only |

Both build the same ~30-line body text, both wrap it in a `Blob`, both click a
synthetic `<a download>`. They are copies, not a shared function.

There is a third surface that mints a secret and offers **no** download at all:
Settings -> Master Password (`dashboard.js:2789` `changeMasterPassword`, fed by
`GET /api/system/suggest-password`).

---

## 2. Findings

### F1 — Duplication, and the stated reason for it is false

Both copies carry a comment justifying the duplication:

- `installing.html:191` — *"Twin of `downloadCredsSheet()` in `static/dashboard.js`;
  this page has no access to that file."*
- `dashboard.js:2719` — *"The twin of this function lives in `installing.html`,
  which has its own inline script and never loads this file."*

Both statements are true of **`dashboard.js` specifically** — a 3000-line file that
assumes the dashboard DOM — and false of **static assets in general**.
`installing.html` already includes `_theme.html`, which emits
`url_for('static', filename='hb.css')`, and `asset_v` is a global context
processor (`src/app.py:467`). A shared `src/static/creds_sheet.js` was available
the whole time.

The duplicated text is the instructions a locked-out owner reads to get back into
their own house. It is the worst thing in the codebase to have two copies of:
a wording fix applied to one copy and not the other is invisible until the day it
matters.

### F2 — A dead parameter, and the surface it was meant for

`downloadCredsSheet({ password, phrase })` in `dashboard.js` accepts a `password`,
but its only call site (`:2763`, `downloadRevealedPhrase`) passes `{ phrase }`.
The `password` branch is unreachable. The surface it was evidently written for —
Settings -> Master Password — ships without a download button; the user is told
*"Suggested — save it somewhere before you submit"* and left to it.

### F3 — The sheet rots silently

After a master-password change the owner's saved `.txt` lists the **old**
password. Nothing warns them and nothing offers a replacement.

The recovery-phrase line, by contrast, stays correct: rotation
(`scripts/rotate_master_password.sh`, both entry points) does not touch the
`RECOVERY_*` keys in `.env`. The two halves of the sheet have different
lifetimes. That asymmetry is the design input for §4.

### F4 — Zero test coverage, and the untested part is the browser

There is no JavaScript test harness anywhere in `scripts/tests/`.
`test_recovery.py` and `test_master_password.py` cover the Python. The
2026-08-01 RPi4 hardware E2E validated rotation, not this button. The download
path — Blob, anchor, `download` attribute, revoke timing — has never been
executed by a test.

### F5 — Mechanical defects in both copies

Cheap to fix; none has ever been exercised, so these are *plausible* rather than
observed:

1. `URL.revokeObjectURL(url)` runs synchronously on the line after `a.click()`.
   Revoking a Blob URL in the same task as the click has historically cancelled
   the download in Firefox. Defer it.
2. The anchor is never inserted into the document. Firefox historically required
   an in-document anchor for a programmatic click on a download link.
3. The body contains **em dashes** (`'Keep this offline — print it'`) in a
   `text/plain` Blob declared with **no charset** and no BOM. A recovery sheet is
   precisely the file someone prints from an unfamiliar machine. Keep it ASCII.
4. Lines are joined with `\n`. `\r\n` is the safer choice for a file whose whole
   purpose is to be opened by an unknown text editor and printed.
5. The filename is **date-only** (`homebrain-recovery-2026-08-20.txt`). Two
   generations on one day collide into `(1)`, `(2)` in the Downloads folder with
   nothing in the name to say which is current.

### F6 — The handover sheet collapses a separation the design deliberately built

`docs/plans/RECOVERY_PHRASE.md` §2 rejects deriving the recovery words from the
master password, on the grounds that it *"makes the recovery sheet equal to the
master password."* The handover download then writes **both secrets into one
unencrypted file in `~/Downloads`** — on a typical laptop, a cloud-synced
directory. The independence that B2 exists to create is preserved on the box and
collapsed on the client.

This does **not** make the download wrong. The alternative is hand transcription,
and transcription errors cause exactly the lockouts this feature exists to
prevent; the same two secrets are already on screen to be photographed. It does
mean the sheet should be built as an artifact that is unpleasant to leave lying
around, not as a convenience dump. See §4.

### F7 — There is no "I lost my phrase" state

`GET /api/recovery/status` reports `configured: true` forever once a phrase is
minted, so the Status-tab nag banner (`dashboard.html:41`) goes quiet
permanently — while the owner may be holding nothing at all. Only the human knows
the sheet was lost, and no surface ever asks.

### F8 — A used recovery phrase is never retired

`POST /api/recovery/reset` neither rotates the phrase nor offers a fresh sheet.
A used break-glass code stays valid indefinitely. That is defensible for a secret
intended to live on a device label — you cannot reprint a label — but it is
nowhere recorded as a decision. Recording it here is the whole fix.

---

## 3. Decisions

**D1 — The handover sheet keeps both secrets.** Setup is the only moment the
master password is ever shown; splitting it into two downloads at the highest-stakes
moment in the product is worse than the `~/Downloads` exposure of F6. Unchanged
from today.

**D2 — The phrase-reveal sheet stays phrase-only.** Unchanged from today.

**D3 — The master-password-change sheet is password-only**, and states explicitly
that the recovery phrase is unchanged. This is the fix for F3 and the use for
F2's dead parameter.

**D4 — The sheet stays client-side, and the reason is narrower than previously
recorded.** `RECOVERY_PHRASE.md:335` says client-side "because only the scrypt
hash is ever stored, so no server endpoint can serve the plaintext." That argues
against a standalone `GET /api/recovery/sheet` and is correct. It does *not*
argue against composing the body server-side inside responses that already carry
the plaintext (`/api/setup/credentials`, `/api/recovery/regenerate`).

The actual binding constraint is D3: the new master password exists only in the
browser form. `POST /api/system/master-password` returns `{status: "started"}`,
and having the server compose that sheet would require echoing the password back
over HTTP. One shared client-side builder serves all three surfaces; a
server-side one serves two and needs a new secret round-trip for the third.

**D5 — F8 is documented, not changed.** Single-use recovery codes are the
textbook answer and the wrong one for a phrase written on a device label.

---

## 4. Design

### 4.1 One module, split pure / impure

New file `src/static/creds_sheet.js`, roughly 65 lines, two functions:

```js
// Pure. No DOM, no globals, no Date.now(). Fully testable under JavaScriptCore.
function buildCredsSheet({ password, phrase, device, now, phraseUnchanged })  // -> string

// Impure. Blob + anchor plumbing only. No wording.
function saveCredsSheet(text, filename)                                       // -> void
```

Keeping `buildCredsSheet` pure — with `device` and `now` injected rather than read
from `location` and `new Date()` — is what makes §6 possible without a browser or
a DOM shim. This is the single most important structural decision in the plan.

### 4.2 Body rules

- ASCII only (`--` for em dashes). Enforced by a test, with a comment saying why,
  or a later editor will "improve" it back.
- `\r\n` line endings.
- Conditional blocks preserved exactly as today:
  - the `Master password:` line only when a password is supplied;
  - the `Recovery phrase:` line only when a phrase is supplied;
  - the "To use the recovery phrase: ... Forgot your password?" paragraph only
    when a phrase is on the sheet. Setup falls back to a password-only handover
    when the wordlist is unavailable, and telling that owner to enter a phrase
    sends them looking for something they were never given.
  - **new:** a "Your recovery phrase is unchanged" line only when
    `phraseUnchanged` is set (D3).

### 4.3 Filename

`homebrain-recovery-YYYY-MM-DDTHH-MM.txt`. Minute resolution kills the F5.5
collision without putting a secret or a hostname in a filename that will show up
in screen shares.

### 4.4 Wiring

`dashboard.html` and `installing.html` each gain one line:

```html
<script src="{{ url_for('static', filename='creds_sheet.js') }}?v={{ asset_v }}"></script>
```

`installing.html` must load it **before** its existing inline script block.
Both inline copies are deleted.

The login gate (`app.py:4986`, `render_template_string`) is deliberately
self-contained and stays that way — it has no download and needs none.

---

## 5. Surfaces after the change

| Surface | Sheet contents | Button |
|---|---|---|
| Setup handover | password + phrase | existing, `installing.html:112` |
| Settings -> Recovery Phrase reveal | phrase | existing, `dashboard.html:975` |
| Settings -> Master Password | password + "phrase unchanged" note | **new**, revealed after `status: "started"` |

---

## 6. Test plan

`scripts/tests/test_creds_sheet.js`, executed with `osascript -l JavaScript`.

This is not a stylistic choice: there is no node, deno, or bun on the development
Mac, and no Chrome or Firefox. JavaScriptCore via `osascript` is the JS runtime
this repo actually has, and because `buildCredsSheet` is pure (§4.1) it runs there
with no DOM shim.

Assertions:

1. phrase-only -> no `Master password:` line; **includes** the "Forgot your
   password?" paragraph.
2. password-only -> no `Recovery phrase:` line; **excludes** the "Forgot your
   password?" paragraph. (This conditional exists today and is exactly the kind of
   thing a refactor breaks silently.)
3. both -> both lines present.
4. `phraseUnchanged` -> the D3 note appears, and no `Recovery phrase:` line.
5. body matches `/^[\x20-\x7e\r\n]*$/` — the ASCII guarantee of §4.2.
6. every line ends `\r\n`.
7. filename matches `/^homebrain-recovery-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}\.txt$/`.

**Regression assertion (the important one):** the test also contains the *current*
`installing.html` body-builder, verbatim, and asserts the new builder's output is
identical to it modulo the ASCII and CRLF normalisations. A broken handover
download costs a factory reset; this is the guard.

Registered in `docs/TESTING.md` alongside the existing suites.

---

## 7. Verification gate

Unit tests cannot reach the part that has never run. All four defects found in the
2026-08-01 RPi4 E2E were of this class — invisible to unit tests, obvious on real
hardware. Layers, weakest to strongest:

- **L1 Pure logic** — `osascript -l JavaScript scripts/tests/test_creds_sheet.js`.
- **L2 Regression** — byte-comparison against the shipped builder (§6).
- **L3 Server integration** — the real Flask app under `hbvenv`, asserting
  `/static/creds_sheet.js` is served with the `asset_v` query, that both templates
  reference it, and that neither template still contains an inline copy.
- **L4 Real browser** — Safari on the development Mac against the local app:
  click each of the three buttons, confirm a file lands, diff its bytes against
  the L1 expectation.
- **L5 Live box** — deploy and repeat L4 against the real handover page.

**L5 has a hard constraint.** `scripts/update.sh:132` hardcodes
`archive/refs/heads/main.tar.gz` for the dev channel, so the manager can only ever
deploy `main` — a branch cannot be pushed to a box. Live verification therefore
requires merging first, which inverts the usual merge gate. The RPi4 test box
(`homebraintest.local`, 192.168.178.51) exists for exactly this and is the correct
venue; `192.168.178.58` tracks **stable** and moving it to dev has its own cost.
Record which box was used and on which channel.

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Handover download breaks | **High** — a lost handover sheet plus a lost password is a factory reset | §6 regression assertion ships **with** the refactor, not after it |
| `creds_sheet.js` fails to load on the setup page (static path differs mid-install) | Medium | L3 asserts it against the real app; L4/L5 click the real button |
| iOS Safari ignores `download` on a Blob URL | Medium — setup is phone-friendly by design | Verify on a phone at L5; the screen still shows both secrets as a fallback |
| ASCII rule quietly reverted later | Low | Test 5 fails the build |

---

## 9. Out of scope

Rejected deliberately, each because it adds surface without removing a defect:

- PDF generation, QR codes, print stylesheets.
- An encrypted or password-protected sheet (chicken-and-egg: the password is what
  the sheet carries).
- `GET /api/recovery/sheet` — correctly rejected already (`RECOVERY_PHRASE.md:335`);
  the box holds only a scrypt hash.
- Emailing the sheet.
- Gating the "I have saved my password — log in" button behind a download. An
  owner may legitimately photograph the screen instead.
- Changing F8's phrase-rotation semantics (see D5).

**F7 is deferred to its own change**, not folded in here: an "I no longer have my
phrase" link in the Recovery Phrase card that expands the existing regenerate
flow. It is a product decision, this is a refactor, and mixing them makes the
regression diff unreadable.

---

## 10. Implementation status

Landed on `refactor/recovery-sheet` (`7b6f112`). `src/static/creds_sheet.js`
(97 lines) replaces both inline copies; `dashboard.js` −45/+18,
`installing.html` −44/+6. All of §2's findings are addressed except F7 and F8,
which §9 defers on purpose.

**L1 + L2 — 25/25** (`osascript -l JavaScript scripts/tests/test_creds_sheet.js`).
Mutation-checked, not merely green: renaming one word in the body ("Anyone" ->
"Anybody") fails 3 assertions, and switching the join back to `\n` fails a 4th.

**L3 — 3/3** (`scripts/tests/test_creds_sheet_wiring.py`), plus the neighbouring
suites unaffected: `test_recovery` 9/9, `test_master_password` 11/11,
`test_setup_credentials` 8/8.

**L4 — all three surfaces, Safari 18.6, real files on disk.** The real app was
run from this checkout with `INSTALL_CREDS_PATH` pointed at throwaway
credentials, so the genuine handover page rendered and its genuine button fired.
Downloads landed as `homebrain-recovery-2026-08-20T21-{16,17,18}.txt`:

| Surface | Verified |
|---|---|
| Handover | password + phrase, 666 bytes, pure ASCII, every newline CRLF, no BOM |
| Phrase reveal | phrase only (minted by the real `/api/recovery/regenerate`), no password line, recovery how-to present |
| Master password | new password + `Recovery phrase:  unchanged`, no phrase, no how-to |

Minute-resolution filenames confirmed distinct across the three runs — the F5.5
collision is gone.

**L5 — partial, and the gap is named.** The RPi4 test box
(`homebraintest.local`, 192.168.178.51) was powered off for the whole session:
ARP incomplete, ssh and http both refused. **A hardware provisioning run of the
handover page has therefore not happened.** That is the one item from §7 still
outstanding.

`192.168.178.58` was verified instead, for the two dashboard surfaces. It is on
`channel: beta, ref: main`, so the four runtime files were hand-deployed
(rollback copy at `/opt/homebrain-rollback-credsheet/`) rather than merged;
all three pre-existing files matched `HEAD~1` byte-for-byte first, so the box was
a clean baseline. After a manager restart: `/static/creds_sheet.js` serves 200 as
`text/javascript; charset=utf-8`, md5-identical to the checkout; the dashboard
carries the tag ahead of `dashboard.js`; no page still holds an inline builder;
both the phrase and master-password download buttons are present.
`version.json` is untouched.

The recovery phrase on `.58` was deliberately **not** regenerated. It is a live
production secret, the endpoint is untouched by this change, and burning it to
re-prove server behaviour that `test_recovery.py` already covers would be a
destructive act with no evidence value.

**Note for whoever picks this up:** the box now runs unmerged code. The next
update from `main` will `rsync --delete` over it, which is expected and fine.

---

## 11. Sources

- In repo: `docs/plans/RECOVERY_PHRASE.md` (the design this extends, esp. §2 and §9),
  `src/templates/installing.html`, `src/static/dashboard.js`, `src/app.py`
  (`/api/setup/credentials`, `/api/recovery/*`, `/api/system/master-password`),
  `scripts/update.sh` (§7's dev-channel constraint).
- [MDN — `URL.revokeObjectURL()` lifetime notes](https://developer.mozilla.org/en-US/docs/Web/API/URL/revokeObjectURL)
- [MDN — the `download` attribute](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/a#download)
