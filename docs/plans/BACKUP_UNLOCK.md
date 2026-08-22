# Backup unlock — recovery phrase opens archives

**Status:** in tree (2026-08-22). Off-hardware tests in CI. Hardware E2E still outstanding.
**Date:** 2026-08-22
**Essence:** Each encrypted backup is wrapped so **either** the master password
current at backup time **or** the recovery phrase can decrypt it. A dead box,
a backup drive, and the recovery sheet become a complete restore kit. Live-box
recovery (Forgot password?) is unchanged.

Related: [`RECOVERY_PHRASE.md`](RECOVERY_PHRASE.md) (live-box reset; §8.2
deferred this and is superseded *for backups only*),
[`RECOVERY_SHEET.md`](RECOVERY_SHEET.md) (sheet copy),
[`DISASTER_RECOVERY.md`](../DISASTER_RECOVERY.md) (dead-box procedure).

---

## 1. Why this exists (the gap)

Two jobs share the word “recovery” and only one of them works.

| Situation | What works today |
|---|---|
| Box still runs, owner forgot the password | Recovery phrase → set a new master password |
| Box is gone, owner has the backup drive | Master password that encrypted **that** archive |

The recovery phrase is a scrypt **verifier**. It can prove the owner knows the
words and rotate live logins. It is not a second key for the archive. GPG
encrypts with `MASTER_PASSWORD` as it stood at backup time
(`backup.sh` step 11). `MASTER_PASSWORD` is deliberately **not** in the
archive (portable instance secrets exclude it). Disaster-recovery docs call
that password “the one thing that cannot be recovered from anywhere.”

That is consistent with [`RECOVERY_PHRASE.md`](RECOVERY_PHRASE.md) §2 (the
phrase must not *equal* the master password) and §8.2 (envelope encryption
deferred as a whole-box KMS). It is the wrong product for the story we sell:
“one secret, one recovery.”

The failure that justifies the work: owner forgets the password, recovers,
box dies before the automatic post-rotation backup finishes. The drive only
has archives sealed with the forgotten password. Recovery cannot open them.
Total loss, on us.

Owner expectation, confirmed 2026-08-22: *the box is gone, I have the backup
drive and the recovery passphrase, I can restore.*

---

## 2. What we are explicitly *not* doing

- **Encrypting the archive twice.** Size. One data key, two wraps in a header.
- **Encrypting backups with the recovery phrase only.** Then the password the
  owner types every day cannot open their own drive.
- **Deriving the wrap key from `RECOVERY_SCRYPT_HASH`.** The hash is a
  verifier, not key material, and it lives on the box / not in a usable form
  on a stolen drive's ciphertext. Wrap keys must be derivable from the
  **typed phrase + salt in the clear header**.
- **Moving all of `.env` into a KMS.** [`RECOVERY_PHRASE.md`](RECOVERY_PHRASE.md)
  §8.2. This plan is envelope encryption for **backup archives**, nothing else.
- **Letting recovery decrypt Vaultwarden user vaults.** Those stay E2E with
  each user's own password. The sheet and the recovery screen already say so.
- **Rewriting historical `.tar.gz.gpg` files.** Legacy archives stay
  master-password-only. The next backup after the wrap key exists is dual-wrapped.
  No silent 80 GB re-encrypt.
- **Changing live-box recovery.** `/api/recovery/reset` still rotates logins.
  This adds a capability; it does not replace one.

Threat-model sentence we are accepting: a stolen backup drive is openable
with the recovery phrase, not only the master password. That is the same as
“the paper in the drawer can save the house.” Anyone holding that paper can
already take over a living box. Vault items remain out of reach.

---

## 3. Design

### 3.1 Envelope

```
random DEK (32 bytes)
    ├─ wrap_master    = AES-GCM( scrypt(master_password, per-archive salt_m), DEK )
    └─ wrap_recovery  = AES-GCM( RECOVERY_BACKUP_KEY, DEK )

archive body = gpg-symmetric(DEK) of the same tar.gz as today
               (identical cipher flags: AES256, s2k-mode 3, SHA512, count 65011712)
```

The bulk ciphertext is still GPG so `backup.sh` / `restore.sh` /
`test_backup_encryption.sh` stay on one well-understood pipeline. The DEK is
high-entropy; GPG's s2k on top is redundant and kept anyway so the gpg
invocation does not drift from the pinned test.

`cryptography` is already a dashboard dependency (`requirements.txt`). Wraps
use `AESGCM` from `cryptography.hazmat`. No new package.

### 3.2 Recovery backup key — minted with the phrase, never from the hash

The live box does not have the phrase at backup time (hash only). So the wrap
key must be **persisted at mint time**, when the phrase is in memory.

Domain-separated from the verifier hash: a **second** 16-byte salt, not
`RECOVERY_SCRYPT_SALT`.

```
RECOVERY_BACKUP_SALT = random 16 bytes, base64
RECOVERY_BACKUP_KEY  = base64( scrypt(normalize(phrase), salt, n=2^15, r=8, p=1, dklen=32) )
```

Same scrypt params as the verifier (`src/recovery.py`). Stored in `.env`
alongside the existing `RECOVERY_*` keys, mode 600, same lifecycle as
`MASTER_PASSWORD`. **Not** copied into `instance_secrets.env`. The salt is
not secret; it is also written into every archive header so a new box can
re-derive the key from a typed phrase without `.env`.

`build_recovery_record` grows these two fields whenever it hashes a phrase
(setup mint, regenerate, and the enablement path in §3.9).

Invariant: the plaintext phrase is still never written to disk.

### 3.3 Archive format (`HBK1`)

Same filename as today: `homebrain_backup_*.tar.gz.gpg`. Off-site listing,
retention, rclone, and the dashboard name match all keep working.

File layout, one blob, publish-by-rename unchanged:

```
HBK1\n
<one JSON object>\n
\n
<raw OpenPGP ciphertext — identical to today's .gpg body>
```

OpenPGP packets start with a high bit set; `HBK1` is ASCII. No collision
with legacy files.

JSON (v1):

```jsonc
{
  "v": 1,
  "alg": "gpg-aes256",
  "kdf": "scrypt$n=32768$r=8$p=1$dklen=32",
  "wraps": {
    "master":    { "salt": "<b64 16B>", "nonce": "<b64 12B>", "ct": "<b64>" },
    "recovery":  { "salt": "<b64 = RECOVERY_BACKUP_SALT>", "nonce": "<b64>", "ct": "<b64>" }
  }
}
```

- `master.salt` is **per archive** (we have the password at backup time).
- `recovery.salt` is the stable `RECOVERY_BACKUP_SALT` (we do not have the
  phrase at backup time; we have the precomputed key).
- If `RECOVERY_BACKUP_KEY` is missing (box never enabled, phrase mint
  failed), omit `wraps.recovery`. That archive is master-only, same as
  legacy from the owner's point of view, but already in `HBK1` so a later
  header re-wrap can add the recovery slot without touching the body.

`BACKUP_ENCRYPT=false` is unchanged: plaintext `.tar.gz`, no header.

### 3.4 Open procedure (`restore.sh`)

Detect: first four bytes `HBK1` → new path; else existing GPG path.

Secrets to try, in order, first success wins:

1. `RESTORE_PASSPHRASE_FILE` (dashboard / wizard), if present
2. `MASTER_PASSWORD` from `.env`

For each secret, against an `HBK1` file:

1. Unwrap `wraps.master` with `scrypt(secret, master.salt)` as the AES-GCM key
2. Else treat secret as a recovery phrase: `scrypt(normalize(secret), recovery.salt)`
   and unwrap `wraps.recovery`
3. Stop. Do **not** feed the secret to GPG as a legacy passphrase on an `HBK1`
   file — the body is encrypted with the DEK, not with the owner's password.

Legacy `.gpg` (no magic): today's `gpg --decrypt --passphrase <secret>`.
Wrong passphrase still fails before any service is stopped.

Helper: `src/backup_crypto.py`, Flask-free, called from bash.

```
python3 src/backup_crypto.py seal    --master-file … --recovery-key … --recovery-salt … --dek-file … --header-file …
python3 src/backup_crypto.py open    --archive … --secret-file … --dek-file …
python3 src/backup_crypto.py inspect --archive …     # magic, wraps present, ciphertext offset
python3 src/backup_crypto.py rewrap  --archive … --master-file … --recovery-key … --recovery-salt …
```

`open` writes the DEK to a 0600 temp file; `restore.sh` / verify pipe it to
`gpg --passphrase-fd` the way they pipe the master password today, then shred.

Wrong-secret handling: AES-GCM auth failure is a miss, try the next unwrap.
Do not distinguish “wrong master” from “wrong phrase” in logs the owner sees.

### 3.5 Password change and live-box recovery reset

No re-wrap. The recovery key is bound to the **phrase**, not the password.

- Old `HBK1` archives: still open with the (unchanged) phrase, or with the
  **old** master password.
- New archives: wrapped with the new master password and the same recovery key.

`rotate_master_password.sh` already starts a full backup under the new
password. After this lands, that backup is also phrase-unlockable, and so
is every dual-wrapped archive from *before* the rotation. The post-rotation
cliff goes away for any archive that has a recovery wrap.

`needs_old_passphrase` in the backup list becomes: encrypted **and**
(legacy GPG, or `HBK1` with no recovery wrap) **and** older than the
rotation epoch. Dual-wrapped archives are not flagged. The restore prompt
copy changes regardless (see §3.8).

### 3.6 Regenerating the recovery phrase

The old phrase must stop opening **new** archives. Logged-in, so we have
the current master password.

1. Mint new phrase, new verifier hash, new `RECOVERY_BACKUP_KEY` / `SALT`
   (existing regenerate, plus the two new fields).
2. For each **local** `HBK1` archive: `rewrap` — unwrap DEK via master wrap,
   replace `wraps.recovery`, rewrite `header + existing ciphertext` to a
   temp name, rename. No re-GPG of the body. Cheap even for 80 GB.
3. Legacy GPG files: leave them. They never had a recovery wrap.
4. Off-site: next scheduled mirror uploads the rewritten locals. No special
  rclone path.

A regenerate that cannot rewrap (missing master wrap, corrupt header) logs
and continues; those archives stay openable with the old phrase until the
next successful backup replaces them.

### 3.7 Wizard restore (dead box — the actual product)

Today (`start_setup`): the typed value **is** the new box's master password.
It must match `NEW_PASSWORD_RE` (no spaces). It is written to
`MASTER_PASSWORD` *before* deploy. `restore.sh` is chained after deploy
**without** `RESTORE_PASSPHRASE_FILE`, so it decrypts with `.env`'s
`MASTER_PASSWORD`. A recovery phrase would be rejected at the charset check
and, if forced through, would become the dashboard password.

That has to split into **unlock secret** vs **box password**.

Discriminator is free: user-chosen and generated master passwords cannot
contain spaces (`NEW_PASSWORD_RE`, B1 is hyphen-joined). Recovery phrases
are space-joined words. A secret with a space cannot be a master password.

| Typed secret | Box master password | Recovery on the new box |
|---|---|---|
| Looks like a master password | Seed it, same as today. Box comes back on the old password. | Mint a **new** phrase (hash was never in the archive — existing deviation). |
| Looks like a recovery phrase (spaces / fails `NEW_PASSWORD_RE`) | **Generate** a new one. `restore.sh` already resets NC admin and HA to `.env`. Handover shows the new password. | **Adopt** the typed phrase: `build_recovery_record(typed)` + backup key. Do not mint a second phrase. |

Always write `RESTORE_PASSPHRASE_FILE` with whatever was typed and pass it
into the chained `restore.sh`, even on the master-password path (then file
and `.env` match; restore tries the file first).

Wizard copy: label becomes “Master password **or** recovery phrase.” Drop
the `NEW_PASSWORD_RE` hard-fail. Empty still refused.

`welcome.html` currently requires a value that “HomeBrain was using.” Change
that sentence. `docs/DISASTER_RECOVERY.md` point 2 becomes: either secret
opens a dual-wrapped archive; legacy archives still need the old master
password.

After a phrase-unlock wizard restore, the handover sheet carries the **new**
master password and the **same** recovery phrase they just typed. Say that
plainly on `installing.html` so they do not think the phrase was rotated.

### 3.8 Dashboard restore (box still runs)

`/api/restore` already accepts an optional passphrase via a 0600 temp file.
Empty still means “use current `MASTER_PASSWORD`.”

Prompt copy:

- Dual-wrapped / current-password era: “Leave empty for the current master
  password. Or enter the recovery phrase, or a previous master password.”
- `needs_old_passphrase` (legacy or no recovery wrap): keep today's warning
  that this archive needs the password from when it was made; mention the
  recovery phrase only if `inspect` says a recovery wrap is present.

Do not make the owner guess. `inspect` is cheap (header only). The backup
list can grow `unlock: "master" | "master_or_phrase" | "legacy"` from a
header peek so the prompt is exact. Skip the peek on off-site listings
until the file is fetched — off-site rows keep a conservative prompt
(“password or recovery phrase”).

### 3.9 Existing boxes (the phrase is already minted)

We cannot compute `RECOVERY_BACKUP_KEY` from the stored hash. Existing
installs need the phrase typed **once** while logged in.

- `GET /api/recovery/status` grows `backup_unlock: bool` (both new `.env`
  keys present).
- Settings → Recovery Phrase: if a phrase is configured and
  `backup_unlock` is false, a form: “Enter your recovery phrase to let it
  unlock backups.”
- `POST /api/recovery/enable-backup-unlock` `{phrase}` — session-gated,
  same LAN/rate rules as regenerate. Verifies against the existing hash
  (constant-time, 2s penalty). On success, writes the two new keys. Does
  **not** rotate the phrase.
- Honest copy: “The **next** backup will open with this phrase. Archives
  already on the drive still need the master password that encrypted them.”
- Optionally start a full backup on success (same detached pattern as
  rotation). Recommended: yes. That is what closes the nightmare window
  on an upgraded box. Failure to start the backup is non-fatal; the keys
  are already stored so the *next* scheduled run dual-wraps.

New provisions skip this: setup mint writes the keys from the start, so
the first backup is dual-wrapped.

### 3.10 Legacy archives

Leave them. Restore path 3.4 already handles raw GPG. The first dual-wrapped
backup after enablement/setup is the one the dead-box story needs. Retention
will age the rest out.

Do **not** take on in-place conversion of legacy GPG → `HBK1` in v1. That
is a full decrypt/re-encrypt per file.

---

## 4. Files to add / change

### New

- `src/backup_crypto.py` — seal / open / inspect / rewrap. Stdlib +
  `cryptography`. No Flask. Unit-tested.
- `scripts/tests/test_backup_crypto.py` — wrap round-trips, wrong secret,
  legacy detect, rewrap keeps body, phrase normalize, truncated header.
- `scripts/tests/test_backup_unlock.sh` — extends the gpg pipeline test:
  `HBK1` body still uses the pinned gpg flags; open with master; open with
  phrase; reject third secret; legacy file still opens with master only.

### Changed

- `src/recovery.py` — `build_recovery_record` emits `RECOVERY_BACKUP_SALT`
  and `RECOVERY_BACKUP_KEY`. Small helper `derive_backup_key(phrase, salt)`.
- `scripts/backup.sh` — after tar\|gpg to a temp **body**, seal header, concat
  `header + body`, verify via `open` with master then `tar -tz`. If no
  recovery key, `HBK1` with master wrap only. Fail closed: if seal fails,
  do not publish a legacy file by accident (owner would think they have
  phrase-unlock).
- `scripts/restore.sh` — detect `HBK1` vs legacy; `open` then gpg with DEK;
  error text covers both secrets.
- `src/app.py` — `start_setup` restore branch (§3.7); recovery status +
  enablement route; backup list `unlock` field; regenerate triggers local
  `rewrap`.
- `src/templates/welcome.html` — label + validation.
- `src/templates/dashboard.html` / `src/static/dashboard.js` — restore
  prompt, recovery-card enablement, `needs_old_passphrase` only when true.
- `src/static/creds_sheet.js` — sheet promises dead-box restore: either
  secret opens backups made after this feature; phrase still does not
  unlock Vault items. Update `scripts/tests/test_creds_sheet.js`.
- `config/.env.template` — document the two new keys; backup-encrypt
  comment.
- `docs/DISASTER_RECOVERY.md` — point 2 rewritten.
- `docs/TESTING.md` — new E2E section.
- `docs/plans/RECOVERY_PHRASE.md` §8.2 — pointer here; no longer “deferred”
  for backups.

### Untouched

- Live recovery verify/reset, LAN guard, rate limits.
- Vault user vaults, `VAULT_DB_PASSWORD`.
- Off-site rclone (whole file, header included).
- `BACKUP_ENCRYPT=false`.
- Filename pattern and retention.

---

## 5. Security checklist (must hold before merge)

- [ ] Phrase plaintext never hits disk. Backup key is derived material, 0600
      `.env`, not in `instance_secrets.env`, not in the archive body.
- [ ] Verifier salt and backup salt are different on every mint.
- [ ] Header contains salts, nonces, and wrapped DEKs only — no phrase, no
      `RECOVERY_BACKUP_KEY`, no `MASTER_PASSWORD`.
- [ ] `enable-backup-unlock` re-verifies the phrase (constant-time, 2s
      penalty); it cannot be used as an oracle distinct from `/verify`.
- [ ] Wizard recovery path does **not** write the phrase into
      `MASTER_PASSWORD`.
- [ ] `rewrap` never re-encrypts the body under a new DEK (avoids a second
      full-archive write and a window where a crash publishes a truncated
      body). Header rewrite uses temp + rename, same as backup publish.
- [ ] `backup.sh` does not fall back to legacy GPG if `HBK1` seal fails.
- [ ] grep of a published archive for the phrase / backup key / master
      password is empty (extend the existing “hash only in archive” idea
      from recovery tests — the archive still must not contain `.env`).
- [ ] Stolen-drive model: opening requires master **or** phrase; there is
      no wrap that uses the verifier hash.

---

## 6. Test plan

### Off-hardware (CI)

1. `test_backup_crypto.py` — master wrap round-trip; recovery wrap
   round-trip; either secret opens the same DEK; third secret fails; inspect
   reports legacy vs `HBK1` vs missing recovery wrap; rewrap changes recovery
   wrap, `cmp` of ciphertext after offset is identical; truncated header
   fails closed.
2. `test_backup_unlock.sh` — `HBK1` file: gpg flags unchanged from
   `test_backup_encryption.sh`; decrypt with master; decrypt with phrase;
   wrong secret; truncated body. A **legacy** file built with the old
   pipeline still decrypts with the master password and does **not** decrypt
   with the phrase.
3. `test_recovery.py` — `build_recovery_record` includes the two new keys;
   backup key ≠ verifier hash; same phrase + same salt recomputes.
4. `test_creds_sheet.js` — dead-box sentence present when a phrase is on
   the sheet; still absent when phrase-only-omitted; Vault disclaimer stays.
5. Flask: `enable-backup-unlock` wrong phrase → 401; right phrase →
   `backup_unlock: true`; wizard restore with a spaced secret does not set
   `MASTER_PASSWORD` to that secret (unit with the existing welcome-wizard
   harness).

### On-hardware E2E (x86 and RPi)

1. Fresh provision → first full backup is `HBK1` with both wraps
   (`inspect`). Restore on the same box with empty passphrase still works.
2. Restore the same archive with the recovery phrase in the dashboard
   prompt (and a *wrong* current-password empty path after rotating? skip —
   instead, pass the phrase explicitly). Data comes back.
3. Change master password → pre-change `HBK1` archive is **not** flagged
   `needs_old_passphrase`; it opens with the phrase; it still opens with the
   old password if typed; the automatic new backup opens with the new
   password and with the phrase.
4. Settings → regenerate phrase → local `HBK1` archives open with the **new**
   phrase and fail the old one; body sizes unchanged aside from header.
5. Enablement on a box provisioned before this ships: status shows
   `backup_unlock: false`; after typing the phrase, next backup is
   dual-wrapped; a leftover legacy archive still needs the old master
   password and says so.
6. Dead-box: nuclear-reset or new hardware → wizard “Restore system” with
   **only** the recovery phrase → setup completes; handover shows a **new**
   master password and the **same** phrase; dashboard / NC / HA accept the
   new password; files from the archive are present.
7. Dead-box control: same wizard with the **master** password → box comes
   back on that password (today's behaviour).
8. `BACKUP_ENCRYPT=false` still publishes plaintext `.tar.gz`.

---

## 7. Phasing & exit criteria

| Phase | Scope | Exit |
|---|---|---|
| **P1 — Crypto + pipeline** | `backup_crypto.py`; `backup.sh` writes `HBK1`; `restore.sh` opens master wrap + legacy; CI tests | New backups dual-wrap when the key exists; same-box restore with current password still works; legacy files still restore |
| **P2 — Phrase as a restore key** | Mint backup key at phrase create; restore `open` recovery wrap; dashboard prompt; backup-list `unlock` / `needs_old_passphrase` | Dashboard restore with the phrase opens a post-ship archive |
| **P3 — Dead box** | Wizard discriminator, `RESTORE_PASSPHRASE_FILE` on the setup chain, adopt-phrase vs mint-phrase, sheet + `DISASTER_RECOVERY.md` | E2E step 6 passes on both architectures |
| **P4 — Existing boxes + regenerate** | Enablement route + UI; optional full backup; regenerate rewrap | E2E steps 4–5 pass |

P1+P2 are shippable on a box that already has the key (new provisions).
P3 is the founder scenario. P4 is the upgrade path. Do not merge P3 without
P1's fail-closed publish rule.

P1 is the gate that must not break nightly backup. Real-hardware: one
scheduled backup, one dashboard restore with empty passphrase, before P2.

---

## 8. Open questions (none blocking)

1. **Start a full backup on enablement?** Default yes (§3.9). If the drive
   is busy or backup is already running, 409 and rely on the schedule — the
   keys are stored either way.
2. **Off-site listing peek.** Conservative prompt is enough for v1. A header
   peek needs a ranged rclone fetch; skip until someone hates the prompt.
3. **Header JSON vs length-prefixed binary.** JSON is greppable and matches
   how the rest of the box talks. Revisit only if we learn of a tool that
   chokes on non-GPG leading bytes in a `.gpg` file (none known; GPG itself
   is only invoked on the sliced body).
4. **Should regenerate fail hard if a local rewrap fails?** No. Partial
   rewrap + log. The next backup is the backstop.
5. **Fernet vs AES-GCM for the wrap.** Fernet is already used for email
   tokens. AES-GCM is the better primitive for a 32-byte DEK and does not
   require the urlsafe-b64 key dance. Pick AES-GCM; do not mix.
)
