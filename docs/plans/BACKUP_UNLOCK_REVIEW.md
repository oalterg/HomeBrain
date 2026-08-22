# Backup unlock — implementation review

**Status:** §1–§3 and the H2 product gap **fixed, verified on hardware, merged**
(PR #205, 2026-08-22). §4 all fixed except L4, which is declined — see §10.
**Reviewed:** [`BACKUP_UNLOCK.md`](BACKUP_UNLOCK.md) design + the implementation in `c47b99a`.
**Verdict at review time:** the crypto core was sound and mergeable. The
pipeline and lifecycle code around it was not — four blockers, two of them
silent data-loss paths, one breaking nightly backup on a small root disk.
**Now:** every §1–§3 finding is fixed, each with a regression test or a
hardware measurement (§8). Two further defects surfaced during the hardware
run and are fixed too (§9).

---

## 0. What was reviewed, and how

Files read in full: `src/backup_crypto.py`, `src/recovery.py`, `scripts/backup.sh`
(steps 11–13), `scripts/restore.sh`, `scripts/common.sh` (env + backup helpers),
the `src/app.py` diff, all changed templates/JS, all changed tests, all changed docs.

Off-hardware suites run locally:

| Suite | Result |
|---|---|
| `test_backup_crypto.py` | 8/8 pass |
| `test_recovery.py` | 11/11 pass |
| `test_master_password.py` | 14/14 pass |
| `test_setup_credentials.py` | 11/11 pass |
| `test_backup_unlock.sh` | **not run** — no `gpg` on the review machine (the script SKIPs cleanly) |
| `test_creds_sheet.js` | **not run** — no `node` on the review machine |

Findings are ordered by severity. Every one carries a `file:line` anchor.

---

## 1. Blockers

### B1 — `backup.sh:446`: the entire encrypted archive is written to `/tmp` on the root disk

> **FIXED** — header written to `$ARCHIVE_TMP`, GPG body streamed onto it; nothing staged off-drive. Measured §8.1.

```bash
SEAL_DIR=$(mktemp -d)                              # backup.sh:446  — no -p, so $TMPDIR (/tmp)
BODY_FILE="$SEAL_DIR/body"                         # backup.sh:451
... | gpg ... -o "$BODY_FILE" 3< "$DEK_FILE"       # backup.sh:469  — full archive, tens of GB
cat "$HEADER_FILE" "$BODY_FILE" > "$ARCHIVE_TMP"   # backup.sh:471
```

Before this change, `gpg -o "$ARCHIVE_TMP"` wrote straight to the backup drive.

`backup.sh:51` sets `STAGING_BASE="$BACKUP_MOUNTDIR"` under the comment *"Use
backup drive to avoid filling OS disk"*. The new code walks straight past that
invariant with the single largest file the box produces.

Consequences:

1. **Root filesystem exhaustion.** The space check at `backup.sh:113-155` only
   measures `df --output=avail "$BACKUP_MOUNTDIR"` (`backup.sh:130`). Nothing
   checks `/`. On a box with ~78 GB of user files, a full backup now needs that
   much free space on the root disk, unchecked. It fills `/` and takes the whole
   stack down mid-backup.
2. **Fatal where `/tmp` is tmpfs** — the body goes to RAM.
3. **Doubles write I/O on every backup**, forever, for no benefit.

This is the same class of failure as the `nofail` mount incident (244 MB of
"backups" silently written to the root disk), except the volume is three orders
of magnitude larger.

**Fix.** Append rather than stage a second copy:

```bash
cat "$HEADER_FILE" > "$ARCHIVE_TMP"
tar -C "$STAGING_DIR" -cz . | gpg ... -o - >> "$ARCHIVE_TMP"
```

At an absolute minimum, `mktemp -d -p "$STAGING_BASE"` — but that still doubles
the drive-space requirement and the I/O, so the append form is the right shape.

---

### B2 — `backup.sh:485`: verification no longer reads the file being published

> **FIXED** — verify runs `copy-body` over `$ARCHIVE_TMP`. Corruption test §8.2.

```bash
gpg --batch --quiet --decrypt --passphrase-fd 3 "$BODY_FILE" 3< "$DEK_FILE" | tar -tz
```

Step 12's own comment states its purpose: *"read the whole archive back through
the full decrypt/decompress pipeline. Catches truncated writes and bad
sectors."* It now reads back the `/tmp` copy instead of `$ARCHIVE_TMP`. A short
write or a bad sector on the backup drive passes verification and gets published
by the `mv` at `backup.sh:503`. The `sync` at `backup.sh:477` is now pointless
on the encrypted path.

The guarantee that the *published bytes* are readable was the entire point of
`BACKUP_VERIFY`, and it is gone.

**Fix.** Verify the artifact:

```bash
"$HB_PYTHON" "$BACKUP_CRYPTO" copy-body --archive "$ARCHIVE_TMP" \
    | gpg --batch --quiet --decrypt --passphrase-fd 3 3< "$DEK_FILE" | tar -tz > /dev/null
```

(This also exercises `copy-body` and the header offset on the real file, which
nothing else does.)

---

### B3 — `backup_crypto.py:47,186`: the `kdf` header field is written and never read

> **FIXED** — `unwrap_dek` derives under `header['kdf']`; `v`/`alg` refused when unknown. Tests `test_open_survives_a_scrypt_cost_bump`, `test_unknown_header_version_is_refused`.

`build_header` (`backup_crypto.py:108`) records
`"kdf": "scrypt$n=32768$r=8$p=1$dklen=32"` via `_kdf_params()`
(`backup_crypto.py:47`). `unwrap_dek` (`backup_crypto.py:186`) then derives both
wrap keys using the **current** `recovery.SCRYPT_*` module constants and never
looks at the stored string. `recovery.derive_backup_key` does the same.

`recovery.py` went out of its way to make the *verifier* survive a cost bump —
`verify_phrase` parses the stored `RECOVERY_PARAMS` (`recovery.py:210`), and
`test_recovery.py::test_verify_honors_stored_params_after_default_bump` pins that
behaviour explicitly. The archive path has the exact opposite property.

**Bump `SCRYPT_N` once and every existing HBK1 archive becomes permanently
unopenable — not just by the recovery phrase, but by the master password too.**
The header already carries everything needed to avoid this; the code just
ignores it.

Related, same function: `v` and `alg` are never validated. A future v2 header
would be parsed as v1 and silently misinterpreted rather than refused.

**Fix.** Parse `header["kdf"]` in `unwrap_dek` (reuse `recovery._parse_params`),
reject unknown `v`/`alg`, and add a test that mirrors
`test_verify_honors_stored_params_after_default_bump`: seal under low params,
bump the module constants, assert the archive still opens.

---

### B4 — `backup_crypto.py:283`: `rewrap_file` resets the archive mtime, and retention sorts by mtime

> **FIXED** — mtime and mode carried onto the replacement. Test `test_rewrap_preserves_mtime_and_mode`; hardware §8.3.

`rewrap_file` writes a fresh temp file and calls `os.replace(tmp, path)`
(`backup_crypto.py:283`). That installs a new inode, so every rewrapped archive
ends up with `mtime = now`, in whatever order `os.listdir` returned
(`app.py:2180` `_rewrap_local_archives`).

Retention is mtime-ordered:

```bash
find "$BACKUP_MOUNTDIR" ... -printf "%T@ %p\n" | sort -rn | awk -v keep="$BACKUP_RETENTION" 'NR > keep {print $2}' | xargs -r rm --
```
— `backup.sh:511`, and `prunable_archives` (`common.sh:1404`) is the same thing
oldest-first for the emergency prune.

After a phrase regenerate, every local archive has an effectively identical
timestamp in arbitrary order. **The next backup's retention pass then deletes
archives at random — including, with high probability, the newest ones.** That is
silent data loss triggered by a Settings button, on the drive the owner is
trusting for disaster recovery.

Second consequence, same cause: mtime **and** size change on every archive, so
the next rclone mirror re-uploads the complete archive set. §3.6 point 4 says
"next scheduled mirror uploads the rewritten locals. No special rclone path" —
that understates it. It is a full re-upload of everything on the drive, which on
a home uplink with a 78 GB archive set is measured in days.

**Fix (minimal).** Capture `os.stat(path)` before the rewrite and
`os.utime(path, (st.st_atime, st.st_mtime))` before `os.replace`.

**Fix (better, and it dissolves B4, the second half of B4, and H1 together).**
When the header already carries a recovery wrap, the replacement header is
*byte-identical in length* — salt, nonce and ciphertext are all fixed-width
base64 of fixed-width inputs, and `json.dumps` with fixed separators is
deterministic. So the common rewrap case can be an in-place `seek(0)` + `write`
of the same byte count: no copy, no new inode, no mtime change, no re-upload,
no temp file. Fall back to the copy path only when the length actually differs
(i.e. adding a recovery wrap to a `master`-only archive for the first time).

---

## 2. High

### H1 — `app.py:4466`: the rewrap runs synchronously inside the regenerate request

> **FIXED** — phrase returned before the rewrap, which now runs as a **subprocess** (a thread was not enough: §9.1). 886 ms on hardware, §8.3.

```python
_rewrap_local_archives(
    get_env_config().get("MASTER_PASSWORD", ""),
    record["RECOVERY_BACKUP_KEY"],
    record["RECOVERY_BACKUP_SALT"],
)
```

§3.6 calls the rewrap "cheap even for 80 GB". It skips the *re-encryption*, but
`rewrap_file` still performs a full read + full write of every archive body. In a
gunicorn worker that is the same failure mode `reconcile_one` was introduced to
dodge: the request times out, the worker is killed, and the user sees a generic
error after their phrase has already been rotated.

Worse, the call sits **inside** the `try` whose `except` returns 500 without the
phrase:

```python
try:
    phrase = recovery.generate_phrase()
    record = recovery.build_recovery_record(...)
    for k, v in record.items():
        update_env_var(k, v)          # <-- .env now holds the NEW record
    _rewrap_local_archives(...)       # <-- if this raises...
except Exception as e:
    return jsonify({"error": ...}), 500   # <-- ...the phrase is never returned
```

`_rewrap_local_archives` swallows per-file exceptions, but `backup_search_dirs()`
and `get_env_config()` are outside that guard. If either raises, `.env` carries a
verifier hash for a phrase **nobody has ever seen** — permanent, silent loss of
recovery. This is the same shape as the "lost session mid-install = permanent
lockout" defect found in the PR #145 hardware E2E.

**Fix.** Return the phrase to the caller first; run the rewrap in a detached
thread (the same pattern `enable-backup-unlock` already uses to kick off a
backup). §8 question 4 already decided rewrap failure is non-fatal — the code
should reflect that decision.

---

### H2 — `creds_sheet.js:75`: the printed sheet promises a restore path the wizard does not have

> **FIXED (both halves)** — the wizard now offers the backup drive as well as the off-site copy, and `POST /api/backups/local/scan` adopts a drive no fstab entry knows about yet, so the sheet's promise is true. Walked end to end on hardware, §8.7.

The sheet now says:

> If the box is gone: on a new HomeBrain, Restore system, and enter
> this phrase (or the master password) to **decrypt the backup drive**.

The wizard restore is **off-site only**:

- `welcome.html:190` / `welcome.html:199` populate the archive picker from
  `/api/backup/offsite` and `/api/backups/offsite/list`. There is no local
  listing.
- `app.py:1139` hardcodes `--from-offsite` into the chained restore command.

There is no path through the wizard to an archive on a physically attached
drive. An owner holding the sheet, a dead box and the backup drive — the exact
scenario quoted in §1 as the confirmed owner expectation, and the exact
sentence used to justify the whole feature — cannot follow the instruction they
were handed.

This matters more than the other copy findings because the sheet is *printed and
filed away*. It is the one artifact that cannot be corrected later.
`DISASTER_RECOVERY.md` is honest — it is framed end-to-end around off-site — so
the sheet is the outlier, not the doc.

Note that `restore.sh` itself handles a local file correctly (`ensure_backup_dir`
at `restore.sh:44`, plus the auto-select-latest branch at `restore.sh:89`). The
gap is purely in the wizard.

**Fix, pick one:**
- extend the wizard to also list archives found on an attached drive and drop
  `--from-offsite` for those; or
- change the sheet (and `welcome.html:111`) to say *off-site backup*, and record
  in §3.7 that drive-in-hand recovery is not covered by v1.

Do not ship the sheet as written.

---

### H3 — `restore.sh:156-158`: a failed header inspection silently degrades to the legacy path

> **FIXED** — only a genuinely absent helper assumes legacy; anything else dies untouched. Hardware §8.4.

```bash
FMT="legacy"
if [[ -f "$BACKUP_CRYPTO" ]]; then
    FMT="$("$HB_PYTHON" "$BACKUP_CRYPTO" inspect --archive "$BACKUP_FILE" --field format 2>/dev/null || true)"
fi
if   [[ "$FMT" == "error" ]]; then die ...
elif [[ "$FMT" == "hbk1"  ]]; then ...
else  # <-- empty FMT lands here
```

Any failure to *run* the inspect — `cryptography` missing from the interpreter
`backup_crypto_python` picked (`common.sh:19`), an import error, a wrong
`INSTALL_DIR`, a stale checkout without `src/backup_crypto.py` — yields an empty
`FMT`, which falls through to the legacy branch and feeds the secret to `gpg` on
an HBK1 file (`restore.sh:192`).

The owner then gets *"Decryption failed — wrong passphrase or corrupt archive"*
while holding the correct recovery phrase, on the flagship path, with nothing in
the log indicating the header was never read. §3.4's rule — "Do **not** feed the
secret to GPG as a legacy passphrase on an `HBK1` file" — is violated exactly
when the box is least able to tell you so.

**Fix.** Treat empty or unrecognised `FMT` the same as `error`, with a distinct
message ("could not read the archive header on this box"). Failing closed here
costs nothing: a genuinely legacy file returns the string `legacy` on success.

---

## 3. Medium

### M1 — phrase plaintext outlives the operation it was written for

> **FIXED** — `DEK_DIR` is in restore.sh's EXIT trap; the wizard's temp file is removed after the chain with the exit status round-tripped. Hardware §8.5.

§3.2 states *"the plaintext phrase is still never written to disk"*, and §5
checklist item 1 repeats it. In practice it is written twice:

- `app.py:1008` — `tempfile.mkstemp(prefix="hb_restore_", suffix=".tmp")`, the
  typed secret written verbatim; removed only by `restore.sh:120`.
- `restore.sh:167` — `try_secret()` writes the secret to `$DEK_DIR/secret`;
  the directory is removed only on paths that reach the `rm -rf`.

Neither is cleaned up when the operation fails. The likeliest failure on a
replacement box is the off-site fetch (`restore.sh:76`, needs working rclone
credentials on brand-new hardware) — precisely the run where the phrase then
sits in `/tmp` indefinitely. A SIGTERM anywhere in the restore does the same;
the `restore.sh:135` trap only covers `TMP_DIR`.

Demonstrated, not theorised: running `test_setup_credentials.py` left four
`hb_restore_*.tmp` files in the temp dir containing
`wobble tundra deputy chrome amulet salsa`. They were deleted during review.

**Fix.** Either register cleanup for both (a `trap`-registered path in
`restore.sh`, and removal in `start_setup`'s failure path / the background task
wrapper), or amend §3.2 and §5 to state the real invariant: *the phrase is never
written to durable box state, and transits a 0600 temp file that the consumer
shreds.*

---

### M2 — `app.py:2283`: off-site listings lost their stale-archive warning

> **FIXED** — epoch comparison restored, `unlock` stays conservative, prompt checks `stale` first. New tests in `test_offsite_progress.py`.

The removed code parsed `ModTime` (RFC3339) and compared it against
`backup_epoch()`, exactly as the local list does. The replacement is:

```python
"unlock": "master_or_phrase" if encrypted else "plain",
"needs_old_passphrase": False,
```

§3.8 asked for a conservative *prompt* on off-site rows, because a header peek
needs a ranged fetch. It did not ask for the epoch flag to be dropped. A
pre-rotation legacy archive sitting off-site now shows the friendly "or recovery
phrase" copy and no warning at all — a strict regression against `main` for the
one case where the owner genuinely needs the old password.

**Fix.** Keep the `ModTime`-vs-epoch computation for `needs_old_passphrase`; make
only the `unlock` value conservative. (`epoch = backup_epoch()` was deleted from
the top of the function and needs restoring.)

---

### M3 — `backup.sh:452` umask window; `SEAL_DIR` absent from the cleanup trap

> **FIXED (trap)** — `SEAL_DIR` is in `cleanup()` and shredded right after verify. The umask half was **withdrawn**: `mktemp -d` already creates the parent 0700, so the file mode inside is not reachable by another user.

```bash
printf '%s' "$MASTER_PASSWORD" > "$MASTER_FILE"   # backup.sh:452 — created at ambient umask
chmod 600 "$MASTER_FILE"                          # backup.sh:453 — too late
```

Same shape as the `update_env_var`/`harden_env_file` leak already documented in
`common.sh:574`. Same pattern for `$SEAL_DIR/rk` and `$SEAL_DIR/rs`. Wrap the
writes: `(umask 077; printf '%s' "$MASTER_PASSWORD" > "$MASTER_FILE")`.

Separately, `cleanup()` (`backup.sh:54`) handles `STAGING_DIR` and `ARCHIVE_TMP`
(`backup.sh:71`) but not `SEAL_DIR`. A SIGTERM/SIGINT between the seal and the
step-12 cleanup leaves the master password, the DEK **and** the recovery wrap key
sitting in `/tmp`. Add `SEAL_DIR` to the trap.

---

## 4. Low / nits

| # | Anchor | Finding |
|---|---|---|
| L1 | `.github/workflows/ci.yml:44-45` | `test_mcp_servers_load.py test_selftest.py` duplicated verbatim on two consecutive lines. |
| L2 | `backup_crypto.py:73` | `passphrase_to_dek` is never called anywhere. Dead on arrival (AGENTS.md §2). |
| L3 | `backup_crypto.py:76` | `hashlib_scrypt_raw` is a public-looking name for a private helper, and does `import hashlib` inside the function body while every other import is at module top. |
| L4 | `backup_crypto.py:95` | `_aes_wrap` passes `None` as AAD. Binding `v\|alg\|kdf` as associated data is one argument and makes the `kdf` field (B3) tamper-evident rather than advisory. |
| L5 | `app.py:2169` | `archive_unlock` maps a corrupt HBK1 header (`format: "error"`) to `unlock: "legacy"`, so the dashboard tells the owner to type their old master password at a file that is actually damaged. `restore.sh:160` gets this right; the UI should match. |
| L6 | `backup_crypto.py:300` | `.hbk1_*.tmp` orphans from an interrupted rewrap land on the backup drive at full archive size. Nothing sweeps them — `backup.sh:104` only removes `.homebrain_backup*.part`. |
| L7 | `backup_crypto.py:282` | `rewrap_file` chmods to `0600`, silently changing archive permissions from whatever `backup.sh` published under. Harmless but inconsistent — pick one mode and set it in both places. |
| L8 | `rotate_master_password.sh:128` | Comment still reads "so every existing archive now needs the OLD password" — false for dual-wrapped archives after this change. |
| L9 | `docs/TESTING.md:317` | Replaces the pinned `must end with "9/9 passed"` assertions with "see the pass count". A real, if small, loss of a regression signal, and outside this change's blast radius (AGENTS.md §3). |
| L10 | `backup_crypto.py:136` | `read_header` scans for the `\n\n` terminator one `f.read(1)` at a time up to `HEADER_MAX` (64 KB), and `copy_body` re-opens the file to redo it. Fine at ~400 bytes, but it runs per-archive on every `/api/backups/list`. |
| L11 | `app.py:4408` | `enable-backup-unlock` starts a full backup gated on `current_task_status["status"] != "running"`, which is a read-then-act race with the task launcher. Benign (worst case the backup does not start, and §3.9 already accepts that), noted for completeness. |

---

## 5. Checked and cleared

Recorded so nobody re-derives them:

- **Base64 padding trap.** `RECOVERY_BACKUP_KEY` (44 chars, one `=`) and
  `RECOVERY_BACKUP_SALT` (24 chars, two `=`) are the first padded base64 values
  consumed by bash other than `HOMEBRAIN_EMAIL_KEY`. Both `export_env_file`
  (`common.sh:463`) and `get_env_config` (`app.py:758`) split on the *first* `=`
  and strip surrounding quotes, so padding survives. Verified empirically by
  round-tripping generated keys through `export_env_file`. The `IFS='='` bug does
  not apply here. `update_env_var`'s sed escaping handles `/` and `+` correctly
  in both the bash (`common.sh:560`) and Python (`app.py:859`) variants.
- **Fail-closed publish (§5).** `backup.sh:462-464` dies on seal failure rather than
  falling back to a legacy file. Correct.
- **No GPG fallback on HBK1 (§3.4 step 3).** Correct in `restore.sh:186` — the
  DEK is what reaches gpg, never the owner's secret. (H3 is a *detection*
  failure, not a violation of this rule.)
- **Secret order on restore (§3.4).** `RESTORE_PASSPHRASE_FILE` first, then
  `MASTER_PASSWORD`, with a same-value short-circuit at `restore.sh:177`.
  Matches the design.
- **Normalization asymmetry.** `_master_wrap_key` deliberately does *not*
  normalize (master passwords are case-sensitive tokens); the recovery path goes
  through `recovery._scrypt`, which does. Seal and unwrap agree on both.
  `test_phrase_normalize_opens` covers the phrase side.
- **Header carries no secrets.** `test_header_has_no_secrets` is correct as far
  as it goes; the archive body still excludes `.env` (`backup.sh:218-228` writes
  only `HOMEBRAIN_EMAIL_KEY` and `HOMEBRAIN_SELF_NONCE` into
  `instance_secrets.env`), so no `RECOVERY_*` value reaches an archive.
- **Wizard discriminator.** `looks_like_recovery_phrase` (`recovery.py:219`) is
  sound: `is_valid_new_password` first, then "contains a space after
  normalization". `NEW_PASSWORD_RE` forbids whitespace and B1 is hyphen-joined,
  so the two classes cannot collide. The 400 on a secret that is neither matches
  today's behaviour, so no regression for exotic legacy passwords.
- **Key reuse across archives.** `RECOVERY_BACKUP_KEY` is a stable AES-GCM key
  reused for every archive, but nonces are 12 random bytes per wrap
  (`backup_crypto.py:96`). Birthday-bound is ~2³² wraps. Not a concern at this
  scale.
- **`enable-backup-unlock` gating.** Session-gated twice (explicitly, and by
  `security_middleware:596` which only whitelists `/verify` and `/reset`), rate
  limited 5/hour, re-verifies with a 2 s penalty. The early "already enabled"
  200 returns before verification but reveals nothing about the phrase. Meets §5.
- **Dead-box service re-credentialing.** On the adopt-phrase path the box mints a
  fresh master password and `restore.sh` re-syncs Nextcloud (`restore.sh:462`),
  Home Assistant (`restore.sh:557`) and the Vault DB user (`restore.sh:491`) to
  `.env`, and `refresh_self_token` re-derives the self-MCP token
  (`restore.sh:336`). The handover screen is therefore truthful. Vault *user*
  vaults stay E2E under their own passwords, as §2 intends.

---

## 6. Test-coverage gap

Every blocker lives in code that no off-hardware test touches:

- `test_backup_unlock.sh` re-implements the seal/gpg/assemble sequence by hand
  rather than driving `backup.sh`, so B1 and B2 are invisible to CI. Nothing
  checks where the body is written or what `BACKUP_VERIFY` actually reads.
- `test_backup_crypto.py::test_rewrap_keeps_body_and_rotates_phrase` asserts the
  ciphertext is byte-identical but never looks at `st_mtime`, so B4 passes.
- Nothing pins the scrypt params of a sealed archive against a later constant
  change, so B3 passes.
- The §6 hardware checklist would catch B1 only on a box whose root disk happens
  to be small enough, and would not catch B4 at all unless a retention pass
  happens to fire after a regenerate.

**Add before hardware E2E:**

1. `test_backup_crypto.py` — rewrap preserves `st_mtime` (and `st_ino`, if the
   in-place fix from B4 is taken).
2. `test_backup_crypto.py` — seal under reduced scrypt params, monkeypatch
   `recovery.SCRYPT_N` upward, assert the archive still opens with both secrets.
3. `test_backup_unlock.sh` — assert `$TMPDIR` holds no file larger than the
   header after a seal (cheap proxy for B1), and that the verify step reads the
   assembled archive.
4. A `restore.sh` case where `inspect` is unavailable, asserting a hard failure
   rather than a legacy-path fallthrough (H3).

---

## 7. Suggested merge order

The design's own phasing (§7) is right; the gate needs tightening:

| Gate | Must hold |
|---|---|
| Before **P1** merges | B1, B2 fixed. P1's stated exit criterion is "must not break nightly backup" — as written, it breaks it on any box whose root disk is smaller than its data set. |
| Before **P2** merges | B3 fixed (or the params bump explicitly forbidden in `recovery.py` with a comment, which is worse). H3 fixed. |
| Before **P3** merges | H2 resolved one way or the other. The sheet is printed; it cannot be recalled. |
| Before **P4** merges | B4 and H1 fixed. Regenerate is the only caller of `rewrap`, and both defects are in that path. |

M1–M3 and the L-list can ride along with whichever phase touches their file.

---

## 8. Hardware verification (homebraintest, RPi4, 2026-08-22)

Box: `192.168.178.51`, aarch64, `/tmp` is **tmpfs 1.9 GB** on 3.7 GB RAM.
Archives ≈ 64 MB. The box was already running `c47b99a` byte-for-byte for every
file under review, so the fixes were overlaid onto it directly.

Corpus used: 4 dual-wrapped `HBK1` archives + 3 legacy `.gpg` archives, with a
real master-password rotation at epoch `1787397243` sitting in the middle of
them — which turned out to be what exposed §9.2.

### 8.1 B1 — A/B on the same box, same workload

| backup.sh | largest file in `/tmp` during the run |
|---|---|
| `c47b99a` (pre-fix) | **64,663,461 B** — `/tmp/tmp.0PNLoQYt7Y/body` |
| fixed | 86,299 B — an unrelated pre-existing file; **zero** seal files |

The pre-fix body went into tmpfs, i.e. RAM. This box survives it at 64 MB; a
box whose archive exceeds ~1.9 GB would not, and the production data set is
78 GB. The space check never looked at `/`.

### 8.2 B2 — verification now reads the published bytes

One byte flipped at offset 5,000,000 of a published archive:

- intact archive through the verify pipeline → **verifies**
- corrupted archive through the same pipeline → **rejected**

Pre-fix this read a pristine `/tmp` copy, so drive corruption passed.

### 8.3 B4 + H1 — regenerate via the real API

`POST /api/recovery/regenerate` → **HTTP 200 in 886 ms**, 6-word phrase
returned. Pre-fix the same call produced `WORKER TIMEOUT` → `SIGKILL` → no
response at all (§9.1).

After the background rewrap finished, for all four `HBK1` archives:

| mtime unchanged | size delta | opens with the NEW phrase |
|---|---|---|
| YES (4/4) | 0 (4/4) | 3/4 — see below |

Retention ordering (`find -printf %T@ \| sort`) intact. The fourth archive
predates the master-password rotation, so its master wrap holds the old
password, the rewrap could not open it, and it was skipped with
`WARNING - Could not rewrap …: master wrap did not open` in `manager.log` —
correct per §3.6 / §8.4 of the plan.

### 8.4 H3 — restore fails closed

| archive handed to `restore.sh` | result |
|---|---|
| header `{"v":2,…}` from a "newer HomeBrain" | dies, *nothing changed* |
| `HBK1` magic + non-JSON header | dies, *nothing changed* |
| helper present but unimportable (`inspect` returns nothing) | dies, *nothing changed* |
| helper genuinely absent (pre-HBK1 checkout) | assumes legacy — correct |

Pre-fix, the first and third silently took the legacy path and told the owner
their passphrase was wrong.

### 8.5 Flagship — dead-archive restore driven by the phrase alone

`RESTORE_PASSPHRASE_FILE` = the recovery phrase, nothing else:

- phrase alone unwrapped the DEK (`open` → DEK)
- full `restore.sh` run completed: `=== Restore Complete From: … ===`
- a marker file written *after* the backup was **gone** afterwards → a real
  wipe-and-restore, not a no-op
- restored Nextcloud tree present; NC / HA / Vault / db all `healthy`
- passphrase file shredded; **0** stray DEK directories

### 8.6 Dead box, drive in hand — the whole product story

Nuclear reset (which preserves `/mnt/backup` by design), then the drive was
unmounted and its fstab entry stripped, so the box looked like replacement
hardware: no `.env`, no `.setup_complete`, no idea a backup drive existed.

1. Provision → dashboard sits at the setup gate (401).
2. Factory login, then *Find my backup drive*: probed, adopted `/dev/sda`,
   wrote the `nofail` fstab entry, mounted, listed 8 archives with `unlock`
   read from their headers.
3. `POST /start_setup` with `source: "local"` and **only the recovery phrase**.
4. Deploy + chained restore completed:
   `=== Restore Complete From: /mnt/backup/homebrain_backup_2026-08-22_14-30-21.tar.gz.gpg ===`

Handover and end state:

| check | result |
|---|---|
| `recovery_adopted` | `True` |
| phrase on the sheet == phrase typed | yes |
| master password newly minted, ≠ phrase, no spaces | yes |
| new password logs into the dashboard | HTTP 200 |
| the phrase is *not* a login password | HTTP 401 |
| adopted phrase verifies against the new box's hash | `True` |
| `RECOVERY_BACKUP_KEY` / `_SALT` written | present |
| Nextcloud data restored, six containers healthy | yes |

The archive was opened with no `.env` on the box at all — the wrap salt comes
out of the header, which is the entire point of §3.2.

### 8.7 Suites

Green on the box (`test_backup_crypto` 11/11, `test_recovery` 11/11,
`test_master_password` 14/14, `test_setup_credentials` 11/11,
`test_offsite_progress` PASS, `test_backup_unlock.sh` 14/14) and locally, plus
`test_creds_sheet.js` 12/12 via `osascript -l JavaScript`.

---

## 9. Found during the hardware run (not in the original review)

### N1 — a detached thread is not detached under gevent

`gunicorn` serves this app with **gevent** workers. `threading.Thread` is
monkey-patched to a greenlet, and ordinary file I/O is not a yield point, so
H1's first fix — return the response, then rewrap in a thread — still pinned
the worker: `WORKER TIMEOUT (pid:120518)`, `sent SIGKILL`, request unanswered.
The response had not even been flushed.

**Fix:** the rewrap crosses a process boundary, like `_launch_master_rotation`
already does — `subprocess.run` per archive, secrets handed over in 0600 files,
never on argv. Waiting on a child *is* a yield point.

**Generalise:** in this codebase, in-process background work is only safe for
things that block on sockets. Anything CPU- or file-bound must be a subprocess.

### N2 — `unlock` advertised a phrase that cannot open the archive

`/api/backups/list` reported the pre-rotation archive as
`unlock: master_or_phrase, needs_old_passphrase: false` — i.e. "leave the
prompt empty" — while it opened with **neither** the current master password
nor the current phrase. §3.5's rule ("dual-wrapped archives are not flagged")
assumes the recovery wrap always belongs to the phrase the owner holds. After a
regenerate that skipped an archive (§8.3), it does not.

**Fix:** the header already stores the wrap's salt. `archive_unlock` compares it
with the live `RECOVERY_BACKUP_SALT`; a wrap from a retired phrase generation is
reported `master`, so the existing rotation-epoch rule flags it. Verified: that
archive now reads `unlock: master, needs_old_passphrase: true`, the other three
still `master_or_phrase`.

---

## 10. Still open

Everything in §1–§3 is fixed and merged (PR #205). What is left:

### Declined, with reasons

- **L4 — no AAD on the AES-GCM wraps.** Now that `unwrap_dek` parses the
  header's `kdf` (B3), tampering with that field only derives a wrong key and
  fails the GCM tag. The attack it would close is a self-defeating DoS by
  someone who already has write access to the archive and could simply delete
  it. Adding AAD would also strand every archive written before the change.
  Revisit only if the header ever grows a field that changes meaning rather
  than cost.

### Hardware E2E not yet walked

The plan's §6 list has eight items. Items 2, 4 and 6 are done (§8), and 1 and 3
are partly covered. Genuinely untested on real hardware:

- **x86.** Everything here was measured on the RPi4 (aarch64). The plan asks
  for both, and the production boxes are x86. Nothing in the change is
  architecture-specific, but the tmpfs sizing that made B1 fatal is
  platform-dependent and worth re-measuring.
- **§6.5 — enablement on a box that predates this.**
  `POST /api/recovery/enable-backup-unlock` has unit coverage
  (`test_master_password.py`) but has never run against a real `.env` with a
  phrase already minted, nor been shown to make the *next* backup dual-wrapped.
  This is the upgrade path for every existing install.
- **§6.7 — dead-box control with the master password.** Unit-tested
  (`test_wizard_restore_with_master_password_seeds_it`); the hardware walk
  covered only the phrase path.
- **§6.8 — `BACKUP_ENCRYPT=false`** still publishing a plaintext `.tar.gz`.
- **The off-site half.** `OFFSITE_ENABLED=false` on the test box, so the
  wizard's off-site branch and M2's stale-flag behaviour were verified by unit
  test only, never against a live remote.
- **Restore with an empty passphrase** (falls back to `MASTER_PASSWORD`) on an
  HBK1 archive — §6.1's second sentence.

### Environment, not code

- **Shadowed archives on the mountpoint.** homebraintest carries 367 MB of
  archives on the *root disk* underneath `/mnt/backup`, dated 2026-08-01 to
  -22 — written while the drive was not mounted, the `nofail` failure this
  repo already knows about. Pre-existing and not touched here. Worth checking
  whether berlin and miami have the same shadowing.
- **Release.** Merged, not tagged or deployed. The upgrade path for a live box
  is §6.5 above, which is the item most worth walking before a release.
