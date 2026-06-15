# HomeBrain Recovery Phrase — Design & Implementation Plan

A memorable, word-based recovery mechanism for the box's root credential. Mints an
**independent** recovery secret (rendered as EFF-diceware words) that can reset the
master password and re-cohere the whole service stack — and, separately, makes the
generated master password itself a word passphrase for day-to-day memorability.

Decision (with the co-founder): **B2 + B1** — independent reset code *and* word-based
master password — with **full-stack rotation** on recovery.

---

## 1. Why this exists (the gap)

The "master password" is not a login password — it is the **root secret of the entire
box**. Tracing the current code:

- At setup, the master password is user-chosen or generated (`secrets.choice`, 16
  alphanumeric chars — `src/app.py:749`), then **fan-out copied verbatim** into six
  `.env` keys (`src/app.py:763-765`):
  `MASTER_PASSWORD`, `MANAGER_PASSWORD`, `NEXTCLOUD_ADMIN_PASSWORD`,
  `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `HA_ADMIN_PASSWORD`.
- It further **derives** two service secrets:
  - Vaultwarden `ADMIN_TOKEN` = `argon2id( sha256(MASTER:NONCE) )`
    (`scripts/provision_vault.sh:65`, recomputed in `_vault_admin_token_plain()` at
    `src/app.py:2357`).
  - OpenClaw gateway token = `sha256(MASTER:openclaw-gateway)[:32]`
    (`scripts/utilities.sh:1358`).
- It is stored **plaintext** in `.env` (mode 600). Dashboard login is a plaintext
  compare against `MANAGER_PASSWORD` (`src/app.py:463`).

**Recovery today: none.** The factory sticker password (`pwgen -s 16` —
`scripts/provision.sh:186`) only authenticates *pre-setup*; once
`is_setup_complete()` is true, login switches to `MANAGER_PASSWORD`
(`src/app.py:455-461`) and the sticker is inert. A forgotten master password leaves
exactly two exits, both bad:

1. SSH in and read `.env` — violates the product's "no SSH required" promise.
2. Nuclear reset (`src/app.py:3137`) — destroys all data.

This plan adds a third, safe path.

---

## 2. What we are explicitly *not* doing (and why)

The literal framing "derive the recovery words **from** the master password" is the
wrong cryptographic shape and is rejected on purpose:

- **Circular.** Computing `words = f(password)` requires already holding the password,
  which is the one thing you've lost on recovery day.
- **One-way `f`** (hash → words) yields a *verifier*, not recoverable key material —
  it can confirm a guess but cannot restore access.
- **Reversible `f`** (words losslessly encode the password) makes the recovery sheet
  *equal to the master password* — anyone reading the device label owns Nextcloud
  admin, MySQL root, Home Assistant, and the Vaultwarden admin token. It strictly
  weakens the box.

A recovery factor must be **independent** of what it recovers, **hashed at rest**, and
**single-purpose**. That is the design below.

---

## 3. Design

### 3.1 Two orthogonal pieces

| | B1 — Word-based master password | B2 — Independent recovery code |
|---|---|---|
| Purpose | Day-to-day memorability of the password you type | Regain control after the password is lost |
| Secret identity | *Is* the master password | A *sibling* secret, minted at the same moment |
| At rest | plaintext in `.env` (unchanged model) | **hash only** (never plaintext on disk) |
| Shown | on the setup success page (like today) | **once**, then unrecoverable |
| Recovers what | n/a | resets master password → re-coheres stack |

They are independent: B1 changes how the *typed* password looks; B2 is the break-glass
path. Shipping both means the user has a memorable login **and** a written-down
fallback that is not the same string.

### 3.2 Word encoding (shared by B1 and B2)

- **EFF large wordlist** — 7776 words, ~12.925 bits/word. Chosen over BIP39 (BIP39 is
  built for checksummed crypto seeds; EFF diceware is built for human passphrases:
  distinct words, autocomplete-friendly, no homophones).
- **Bundled** at `res/eff_large_wordlist.txt` (the box is offline at recovery time —
  no network fetch). Public domain / CC-BY; record provenance in the file header and
  a `RECOVERY_WORDLIST_VERSION` constant so future list swaps stay verifiable.
- Words selected with `secrets.choice`. Rendered space-joined, lowercase.
- **Lengths:**
  - B1 master password: **6 words ≈ 77.5 bits**.
  - B2 recovery code: **6 words ≈ 77.5 bits** default (offer 8 ≈ 103 bits as a
    "paranoid" toggle). 6 is the memorability/strength sweet spot the brief asked for.
- **Input normalization** on verify: lowercase, NFKC, collapse internal whitespace,
  strip. Accept space- or hyphen-separated. Be liberal in what we accept.

### 3.3 Recovery code at rest

Never store the plaintext phrase. Store a slow-KDF hash + parameters:

```jsonc
// in factory_config (mode 600) — survives data-safe re-provision, like NEWT_*
RECOVERY_SCRYPT_SALT      = <16 random bytes, base64>
RECOVERY_SCRYPT_HASH      = <hashlib.scrypt(phrase, salt, n, r, p, dklen=32), base64>
RECOVERY_PARAMS           = "scrypt:n=32768,r=8,p=1,dklen=32"
RECOVERY_WORDLIST_VERSION = "eff_large_v1"
RECOVERY_WORD_COUNT       = 6
RECOVERY_CREATED_AT       = <epoch>
```

- **`hashlib.scrypt` (Python stdlib) — zero new dependencies.** Note the existing
  `argon2` is the apt **CLI**, not a Python lib (`requirements.txt` has neither
  `argon2` nor `bcrypt`). Stdlib scrypt keeps the verifier dependency-free.
- Starting params `n=2^15, r=8, p=1` ≈ tens of ms per verify — fine for a
  rate-limited endpoint, painful for offline brute force of a 77-bit secret.
- Storing in **factory_config** reuses `get_factory_config()` plumbing, is already
  mode 600, and (per the idempotent-repoint design) is the durable identity that
  survives a data-safe re-provision — so recovery survives re-provision too. The hash
  in a backup archive is acceptable (it's a hash, gated by a 77-bit preimage + slow KDF).

### 3.4 Endpoints & gate

The recovery flow must work while the user is **locked out**, so it bypasses the
session gate the same way `/login` does (`src/app.py:437`). Add to the
`security_middleware` whitelist (`src/app.py:425-447`):

- `POST /api/recovery/verify` — body `{ "phrase": "..." }`. Normalizes, recomputes
  scrypt, **constant-time** `hmac.compare_digest` against `RECOVERY_SCRYPT_HASH`
  (the pattern already at `src/app.py:3152`). Returns a short-lived, single-use
  reset ticket on success; 2s penalty + generic error on failure
  (mirror `src/app.py:468`).
- `POST /api/recovery/reset` — body `{ "ticket": "...", "new_password": "..." }`.
  Triggers the full-stack rotation (§3.5) as a background task.
- `GET  /api/recovery/status` (authenticated) — whether a recovery phrase exists +
  `RECOVERY_CREATED_AT`. Never returns the phrase.
- `POST /api/recovery/regenerate` (authenticated) — mints a **new** phrase, shows it
  once, replaces the hash. (The old phrase can never be re-shown — only the hash is
  stored. Settings offers *regenerate*, not *reveal*.)

**Rate limiting / exposure (security-critical):**

- `/api/recovery/verify` gets a strict bucket distinct from the dashboard default of
  `2000/min` (`src/app.py:411`) — e.g. `5 per hour` per IP, plus a global lockout
  counter with exponential backoff. This is a full-takeover path; treat it like one.
- **LAN-origin only by default.** A reset reachable over the Pangolin tunnel, gated
  only by a phrase + rate limit, is real internet-facing takeover surface. Restrict
  `/api/recovery/*` to RFC1918 + `homebrain.local` origins (extends the existing
  "Split-Horizon Auth Middleware" — `src/app.py:424`). Remote recovery is an explicit
  opt-in toggle in Settings.
- On any successful reset, fire a notification email via the existing
  `HOMEBRAIN_EMAIL_KEY` path (out-of-band alert that the root credential changed).

### 3.5 Full-stack rotation (the hard part)

A `.env` rewrite alone **desyncs** from the live databases — the six fan-out passwords
are active inside running services. Recovery (and any future "change password") must
mutate the live services in a correct, idempotent order. New script
`scripts/rotate_master_password.sh`, sourced like `provision_vault.sh`, run as a
background task using the existing process model (JSON status in `/tmp`, frontend polls
`/api/task_status` — see AGENTS.md "Process model").

Ordered, each step idempotent and reversible-until-committed:

1. **Authenticate with the OLD password.** Read current `.env` values up front; keep
   them in memory for the live `ALTER`/reset calls. Do **not** overwrite `.env` yet.
2. **MariaDB** — `ALTER USER 'root'@'%'` / `'root'@'localhost'` and the Nextcloud DB
   user (`MYSQL_PASSWORD`) `IDENTIFIED BY <new>`, authenticating with the old root
   password. The Vaultwarden DB user is **untouched** — its `VAULT_DB_PASSWORD` is
   random and independent of the master (`provision_vault.sh:48`).
3. **Nextcloud** — `occ user:resetpassword --password-from-env admin` (via
   `OC_PASS`), then `occ config:system:set dbpassword` to match the new
   `MYSQL_PASSWORD`, run inside the `nextcloud` container.
4. **Home Assistant** — `hass --script auth change_password <admin> <new>` in the
   `homeassistant` container. ⚠️ This is the most fragile step; verify the exact
   invocation on real hardware (HA's auth CLI is version-sensitive).
5. **Atomic `.env` commit** — only after 1–4 succeed, rewrite all six keys via the
   existing atomic `update_env_var` (mkstemp → rename), then re-run the deterministic
   re-derivations: Vaultwarden `ADMIN_TOKEN` (re-run vault provisioning bits) and the
   OpenClaw gateway token (`patch_openclaw_config`). Restart `vaultwarden` and the
   gateway so the new derived tokens take effect.
6. **Invalidate sessions** — `session.clear()` (per `src/app.py:3184` precedent) so
   the old session can't ride through; user logs in fresh with the new password.
7. **Rollback discipline** — until step 5 commits, the old password still works
   everywhere; a failure in 1–4 aborts with `.env` unchanged so the box stays
   loginable. Surface partial-failure state explicitly in the task status.

> Must-state UX truth: recovery restores **administrative control of the box**. It does
> **not** decrypt per-user Vaultwarden vaults — those are end-to-end encrypted with each
> user's own master password (VAULT_PLAN.md §3) and are intentionally unrecoverable from
> the admin side. The first-run modal and the recovery screen must say this plainly.

### 3.6 B1 — word-based master password

Independent of B2: at `src/app.py:747-749`, when generating a master password, emit a
6-word EFF passphrase instead of 16 random chars. Shown on the existing success page /
`install_creds.json` (`src/app.py:752-760`), wiped by `cleanup_credentials`
(`src/app.py:498`) like today. No change to storage or comparison semantics — it's just
a friendlier string in the same slot. User-supplied passwords are unaffected.

---

## 4. Files to add / change

### New
- `res/eff_large_wordlist.txt` — bundled 7776-word list + provenance header.
- `src/recovery.py` — phrase generation, normalization, scrypt hash/verify, ticket
  mint/check. Pure, unit-testable, no Flask imports.
- `scripts/rotate_master_password.sh` — the ordered live rotation (§3.5), sourced by
  recovery reset and reusable by a future "change password" feature.
- `src/templates/_recovery.html` — "Forgot password?" entry on the login gate +
  the one-time phrase reveal modal + Settings "Regenerate recovery phrase" card.
- `scripts/tests/test_recovery.py` — unit tests (see §6).

### Changed
- `src/app.py` — new `/api/recovery/*` routes; whitelist them in
  `security_middleware` (`:425-447`); strict limiter bucket + LAN-origin guard; mint
  recovery phrase during setup alongside the master password (`:743-765`); B1 word
  passphrase generation (`:749`).
- `scripts/provision.sh` / setup path — write the initial `RECOVERY_*` fields into
  factory_config when the master password is first set.
- `config/.env.template` / factory_config docs — document the new `RECOVERY_*` fields.
- `scripts/backup.sh` / `restore.sh` — ensure factory_config's `RECOVERY_*` fields are
  carried (hash only; confirm no plaintext leaks into the archive).
- `docs/TESTING.md` — new E2E section (§6).
- `README.md` / `docs/ROADMAP.md` — feature row; move planned → shipped on release.

### Untouched
- Vaultwarden DB credentials (`VAULT_DB_PASSWORD`) — independent of the master.
- Per-user Vaultwarden vaults — E2E, not in scope for recovery by design.

---

## 5. Security checklist (must hold before merge)

- [ ] Recovery phrase plaintext is **never** written to disk — only `scrypt` hash +
      salt + params in factory_config (mode 600, `homebrain`-owned).
- [ ] `/api/recovery/verify` uses `hmac.compare_digest`; failure path has a fixed 2s
      penalty and a generic error (no oracle on which field was wrong).
- [ ] `/api/recovery/*` has its own strict rate-limit bucket + global lockout/backoff,
      separate from the dashboard polling default.
- [ ] `/api/recovery/*` is LAN-origin-only unless the remote-recovery toggle is on.
- [ ] Reset ticket is short-lived, single-use, and bound to the verifying session/IP.
- [ ] Rotation keeps the box loginable on partial failure (`.env` committed only after
      all live `ALTER`/reset steps succeed).
- [ ] No `shell=True` anywhere in `rotate_master_password.sh` callers (CLAUDE.md
      invariant); atomic `update_env_var` for the `.env` rewrite.
- [ ] Successful reset emits an out-of-band notification (HOMEBRAIN_EMAIL_KEY).
- [ ] Recovery screen + first-run modal state that recovery does **not** decrypt E2E
      Vaultwarden user vaults.
- [ ] Backup archive carries the hash only; verified no plaintext recovery material.

---

## 6. Test plan (additions to TESTING.md)

E2E on the production target (`homebrain@192.168.178.58`) and the RPi (no-GPU) edition:

1. Fresh provision → success page shows a 6-word master passphrase (B1) **and** a
   distinct 6-word recovery code (B2). Both differ; recovery code is shown once.
2. Re-load the success page after `cleanup_credentials` → neither secret is retrievable.
3. Log out; from the LAN, "Forgot password?" → enter the recovery code → set a new
   master password → full rotation runs → log in with the new password.
4. After step 3: Nextcloud admin login works with the new password; HA admin login
   works; MySQL root + NC DB user authenticate; Vaultwarden admin SSO button works
   (token re-derived); OpenClaw gateway reachable (token re-derived).
5. Per-user Vaultwarden vault from before step 3 still requires its **own** unchanged
   user password and still decrypts — proving recovery didn't touch E2E vaults.
6. Wrong recovery code → generic error + 2s delay; exceed the bucket → locked out with
   backoff; verify the lockout is global, not just per-IP-trivially-bypassed.
7. Recovery attempt over the Pangolin tunnel with the remote toggle **off** → refused;
   with it **on** → allowed (and email alert fires).
8. Kill `rotate_master_password.sh` mid-run (between steps 2 and 5 of §3.5) → box stays
   loginable with the **old** password; status reports partial failure; re-run is
   idempotent and completes.
9. Settings → "Regenerate recovery phrase" → new code shown once, old code no longer
   verifies.
10. `backup.sh` → restore on a wiped box → recovery code still verifies (hash carried
    in factory_config); grep the archive for the plaintext phrase → absent.

---

## 7. Phasing & exit criteria

| Phase | Scope | Exit |
|---|---|---|
| **P1 — Mint + store + B1** | EFF wordlist bundled; `src/recovery.py`; mint phrase + word-based master password at setup; show once; store scrypt hash; Settings status/regenerate | Fresh provision shows both phrases once; hash in factory_config; unit tests green |
| **P2 — Verify + dashboard recovery** | `/api/recovery/verify` + reset of `MANAGER_PASSWORD` only; gate whitelist; rate-limit + LAN-origin guard | Locked-out user regains **dashboard** login via the code on the LAN |
| **P3 — Full-stack rotation** | `rotate_master_password.sh`; live NC/HA/MariaDB rotation + vault/gateway token re-derivation; email alert; rollback discipline | TESTING.md §6 steps 3–8 pass on **both** x86 and RPi hardware |

P1+P2 close the lockout gap with low risk. P3 restores the "one password rules all"
invariant and is the gate to merge to `main` (touches provisioning + services →
real-hardware E2E required per AGENTS.md).

---

## 8. Open questions / future hardening

1. **Fan-out blast radius.** One password driving NC admin + MySQL root + HA + vault
   token is itself a smell. Out of scope here (recovery must preserve current
   behavior), but a future plan should consider per-service random secrets with only
   the *dashboard* login tied to the master + recovery code — shrinking what a leaked
   label exposes.
2. **Envelope encryption (the "purest" B-variant we deferred).** A real data-encryption
   key wrapped by *both* the password and the recovery phrase would make recovery
   cryptographic rather than a verifier-reset. It's a large rearchitecture versus
   today's plaintext `.env` and is not justified at home-box scale yet — revisit if we
   ever move secrets out of `.env` into a KMS/keyring.
3. **HA auth CLI fragility.** Step 4 of §3.5 is the version-sensitive one; pin and
   verify per HA image bump in `config/versions.json`.
4. **Word count default.** 6 words (≈77 bits) is the proposed default; confirm whether
   the device-label form factor warrants offering 8 (≈103 bits) as the standard rather
   than a toggle.

---

## 9. Implementation status & deviations (`feat/recovery-phrase`)

**Implemented & tested (P1 + P2):** `src/recovery.py` (9/9 unit tests),
`scripts/rotate_master_password.sh` (shellcheck-clean, `bash -n` OK), the
`/api/recovery/*` routes + gate whitelist + LAN guard + setup minting + B1, the
login-gate "Forgot your password?" flow, the setup-handover phrase reveal, and a
Settings "Recovery Phrase" card. A Flask test-client integration run (13 checks)
covers verify/reset/regenerate, the LAN-origin 403, immediate `MANAGER_PASSWORD`
restore, session-clear-on-reset, and the gated 401 UI.

**P3 (full-stack rotation)** is written but, per AGENTS.md, is **unverified** until
it runs on real x86 + RPi hardware (no DB/NC/HA containers in the dev sandbox).
It is the merge gate.

Deviations from the design above, with rationale:

1. **Storage is `.env`, not factory_config.** The recovery hash lives in `.env`
   alongside `MASTER_PASSWORD`, via the existing `update_env_var`. This makes it
   inherit `MASTER_PASSWORD`'s exact lifecycle — including the backup rule that
   per-install secrets "stay with the box" (`backup.sh` §"Portable instance
   secrets"). Result: it survives a same-box restore, and a cross-instance
   migration correctly mints a *fresh* phrase (the new box has a different master
   password). **No `backup.sh`/`restore.sh` change is needed** — superseding §4's
   note.
2. **The internal Nextcloud↔DB password (`MYSQL_PASSWORD`) is NOT rotated.**
   Rotating it would deadlock `occ` (which needs DB access to rewrite its own
   stored DB password). It is plumbing the user never types; leaving it keeps
   NC↔DB consistent. Only MySQL **root**, the NC **admin login**, HA, and the
   derived tokens rotate. The "one password rules all" invariant is preserved for
   every *user-facing* credential.
3. **No reset ticket.** §3.4's two-step verify→ticket→reset was dropped in favour
   of a stateless design: `/reset` re-verifies the phrase itself (the phrase is
   the capability). This is simpler and avoids ticket state across gunicorn
   workers — `/verify` is now purely a UX precheck.
4. **Email alert → security log.** The reset logs a `SECURITY:` event rather than
   sending email; wiring `HOMEBRAIN_EMAIL_KEY` send is left as a follow-up to
   avoid shipping an unverified mail path.
5. **No `_recovery.html`.** The UI lives inline in the existing surfaces (the
   `render_template_string` 401 gate, `installing.html`, `dashboard.html`) to keep
   the break-glass login page self-contained, as its existing comment requires.

---

## 10. Sources

- [EFF Diceware wordlists & rationale](https://www.eff.org/deeplinks/2016/07/new-wordlists-random-passphrases)
- [Python `hashlib.scrypt` (stdlib)](https://docs.python.org/3/library/hashlib.html#hashlib.scrypt)
- [NIST SP 800-63B — memorized secrets & recovery](https://pages.nist.gov/800-63-3/sp800-63b.html)
- [Diceware passphrase entropy](https://en.wikipedia.org/wiki/Diceware)
- In-repo: `docs/plans/VAULT_PLAN.md` (master-password integration model),
  `scripts/provision_vault.sh` (token derivation), `src/app.py:743-765` (fan-out),
  `scripts/utilities.sh:1355-1360` (gateway token).
