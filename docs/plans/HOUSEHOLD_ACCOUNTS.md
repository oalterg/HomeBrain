# Household accounts — one password per person, across the whole box

The product promise on the README's first line is "a single password". Today that is true for
exactly one person: the owner. Everyone else in the house gets a Nextcloud account and nothing
else, and adding them to the Vault or Home Assistant means visiting two more UIs with two more
concepts of identity.

This extends the promise from the owner to each person: **one memorizable password per person,
which opens their files and, if the owner ticks the boxes, their vault and the house.** Files are
the default. Vault and Home Assistant are opt-in, off until ticked — a photo-backup account for a
child should not mint a Bitwarden vault they will never open.

`docs/plans/TIER2_AND_PHASE0.md:121` deliberately left the direction to the user. It has now been
answered: a household member is a **real account in each service, provisioned from one dashboard
form**. The decisions taken with it are in §2.

---

## 1. What exists today

| Piece | Where | State |
|---|---|---|
| Member = a Nextcloud user, nothing more | `src/app.py:4856` (comment), `:4936`–`:5117` | Shipped (#157) |
| Add member + phone QR in one step | `add_household_member` `src/app.py:5040` | Shipped |
| New code for a member who lost theirs | `repair_household_member` `src/app.py:5078` | Shipped |
| Quota, devices, delete | `:5008`, `:4991`, `:5109` | Shipped (#168) |
| Owner's account can never be paired | `pairing_payload` `src/app.py:4811` | Shipped (#167) |
| Memorable password generator | `recovery.generate_password` (EFF diceware, hyphen-joined) | Shipped |
| Vault admin JWT + `/admin` proxy | `_vault_admin_jwt` `src/app.py:3661`, `_vault_proxy` `:3705` | Shipped |
| HA managed-vs-adopted verdict | `ha_load_account_record` `scripts/common.sh:1122` | Shipped (#164) |
| HA login prove | `ha_login_works` `scripts/common.sh:1086`; `selftest.check_ha_password` | Shipped |
| Shared credential-sheet builder | `src/static/creds_sheet.js:43` | Shipped (#203) |

Everything this feature needs to talk to the three services already exists in the tree. Nothing here
adds a runtime dependency, a container, a port, or a backup surface.

The dashboard copy at `src/templates/dashboard.html:548` currently states the opposite of what we
are about to build ("Nobody else gets the dashboard, Home Assistant or the Vault"). It has to change
with the code, and the *dashboard* half of that sentence stays true forever.

---

## 2. Decisions

**D1 — HomeBrain derives the Vault keys itself.** Vaultwarden is zero-knowledge: no server-side
endpoint can set a master password. Either we do Bitwarden's client-side crypto (§3.4) or the
member picks their own vault password in a browser, which would make "one password" false on the
day it shipped. We do the crypto. It is stdlib + `cryptography` (already a dependency), it is pure,
and it is provable by logging in with the result.

**D2 — Vault and Home Assistant are opt-in per person, off by default.** Files are what every
member gets; the other two are a household decision. HA has no per-entity permissions: a non-admin
member can see and control every device in the house. The HA checkbox is only offered when
HomeBrain owns the HA password (`HA_PASSWORD_MANAGED=true`) — the verdict from #164 gates this for
free, and where the answer is "self-managed" HomeBrain cannot mint an admin token anyway.

**D3 — Vault identity is synthetic and stable: `<uid>@homebrain.local`.** The email is the PBKDF2
salt, so it can never change without re-keying the account. Deriving it from the Nextcloud uid means
no email to ask for, one id across three services, and immunity to a tunnel-domain change. Nothing
is ever mailed to it (`SIGNUPS_VERIFY=false`, no SMTP on the vaultwarden container).

**D4 — No new state file for the roster.** See §3.1. This is the decision that keeps the feature
small. The single exception is the one secret that cannot be recomputed from the services — the
password HomeBrain last issued for that person, sealed, §5.

**D5 — Seal the issued password, not the vault key. Reset through the public API.** A member has
one password, so "they forgot it" must be one repair. Files and Home already are recoverable. The
vault only becomes so if HomeBrain can log in as them and call Vaultwarden's own password-change
endpoint — which means keeping a sealed copy of the password it minted. It does. The cost, and
the limit, are stated in §5.6 rather than buried.

The alternative — escrow `user_key` and `UPDATE` Vaultwarden's `users` table — survives the member
changing their vault password in the Bitwarden app. For a household of four that is the rare case.
It costs two couplings to an undocumented schema (the write, and a cipher-row MAC walk to keep a
chip honest). We take the common case: they lost the sheet. If they changed the vault password
themselves, the chip says so and we refuse to touch that vault.

---

## 3. Design

### 3.1 The roster is derived, not stored

The tempting design is a `members.json` holding each person and their per-service accounts. Reject
it. It can only ever be a cache of what the three services already know, it drifts the moment
someone is deleted in a service's own UI, and it becomes a new thing to back up, restore, migrate
and repair.

Instead, **the roster is the union of what the three services report**, recomputed on each read:

| Service | Query | Match key |
|---|---|---|
| Nextcloud | `occ user:list --info` (already used, `nc_occ_json`) | uid — **the canonical id** |
| Vault | `GET /admin/users` with the existing admin JWT | `email == "<uid>@homebrain.local"` |
| Home Assistant | `config/auth/list` over WS | the `homeassistant` credential's `username` — **phase 0 confirms `config/auth/list` returns it** |

A row is a person: `Alex — files ✓ · vault ✓ · home ✗`. Nextcloud is the anchor because it is the
one service every member has and the one id that cannot be renamed.

The list also carries a **`sealed`** chip, which is not the same claim as `recoverable ✓` — see
§5.5. One means a blob exists; the other means we logged in and it worked.

Consequences, all good:

- A hand-made account appears in the list the moment it exists. There is no "import" step.
- Deleting an account in Nextcloud's own UI cannot leave a stale row here.
- There is nothing to migrate when this ships, and nothing to restore after a disaster.
- No record exists of "HomeBrain manages this member", because none is needed: the invariant is
  **HomeBrain only writes to a service when the owner clicks something for that person and that
  service** (§8). Ownership does not need to be remembered if it is never assumed.

Make the merge a pure function — `merge_roster(nc_users, vault_users, ha_users, owner_uid)` — so the
interesting logic is unit-testable without a single container. Lives in `src/household.py` with the
HA WS helpers, not in the household section of `app.py`.

**Degrade per service.** Today `GET /api/household/members` is one `occ`. After this it is `occ` +
vault admin + HA login-and-WS, and HA down must not 503 the card. The payload carries
`errors: { vault?: "...", ha?: "..." }`; missing services render as unknown, not as ✗. ✗ means
"we asked, they are not there."

**Cache the HA token in the manager session**, the same way `_vault_admin_jwt` already is. An HA
access token from `/auth/token` lives long enough that minting a fresh `login_flow` on every
Household-tab open is a tax, not a security property. Refresh ~30s before expiry. Writes reuse
the cached token. If `HA_PASSWORD_MANAGED != "true"`, skip the HA query entirely.

### 3.2 The password: minted once, shown once, sealed

`recovery.generate_password()` — six EFF diceware words, hyphen-joined, ~77 bits. Its charset
(`[a-z-]`) is already documented as safe through `.env`, Compose, MariaDB, `occ` and the HA auth CLI
(`src/recovery.py:60`). It is generated in the request that creates the person, applied to each
chosen service, returned once for the handover sheet, and written nowhere except the sealed blob
in §5.

That is the same model as the master password, with one extra: the sealed copy lets a later
"add them to the Vault" reuse the string instead of minting a second password for the same person.
There is still no "remind me" — the owner never sees it again — only "issue a new one", which is a
separate, loud route (§3.7).

### 3.3 Nextcloud — unchanged

`user:add --password-from-env`, default quota asserted first, photo settings asserted, app password
minted and drawn as a login QR. All of it already exists and is not touched beyond being called
from the new lockstep path.

### 3.4 Vault — the crypto HomeBrain has to own

Two calls to Vaultwarden, both verified against the pinned 1.36.0 image (§13):

1. `POST /admin/invite {"email": "<uid>@homebrain.local"}` with the admin JWT
   (`_vault_admin_jwt`). Creates a `User` row with an empty password hash plus an `Invitation` row.
   No mail is sent (no SMTP configured), which is exactly what we want.
2. `POST /identity/accounts/register` on `_vault_base_url()` (127.0.0.1, no TLS, no host checks)
   with the payload below. `_register` accepts it because `Invitation::take()` succeeds — **so this
   works with `SIGNUPS_ALLOWED=false` and never needs `_enforce_vault_signups_lockdown`
   (`src/app.py:376`) relaxed.** `INVITATIONS_ALLOWED=true` is already set in `docker-compose.yml`;
   no compose change.

New module **`src/vault_account.py`** — pure, Flask-free, stdlib + `cryptography`, mirroring the
precedent set by `recovery.py` and `backup_crypto.py`:

```
email      = uid + "@homebrain.local"          # lowercased, stripped — it is the salt
master_key = PBKDF2-HMAC-SHA256(pw, email, iterations=600_000, dklen=32)
mp_hash    = b64(PBKDF2-HMAC-SHA256(master_key, pw, iterations=1, dklen=32))
stretched  = HKDFExpand(master_key, info=b"enc", 32) || HKDFExpand(master_key, info=b"mac", 32)
user_key   = os.urandom(64)                    # 32 enc || 32 mac
key        = encstring2(user_key, under=stretched)
rsa        = RSA-2048
keys       = { publicKey: b64(SPKI DER),
               encryptedPrivateKey: encstring2(PKCS8 DER, under=user_key) }

encstring2(plain, under) -> "2." + b64(iv) + "|" + b64(AES-256-CBC(enc_key, iv, plain))
                                 + "|" + b64(HMAC-SHA256(mac_key, iv || ct))
```

`stretched` is RFC 5869 **Expand only**, Bitwarden's `hkdfExpand` on the master key. Use
`cryptography.hazmat.primitives.kdf.hkdf.HKDFExpand`. `HKDF` (Extract-then-Expand) produces a
different stretched key, a register that 200s, and a vault nobody can open. Phase 0 pins this
in the same transcript as the field names.

Register body: `email`, `name`, `masterPasswordHash`, `masterPasswordHint: null`, `key`, `keys`,
`kdf: 0` (PBKDF2), `kdfIterations: 600000`. Field names (`key`/`keys` vs `userSymmetricKey` /
`userAsymmetricKeys`) are confirmed in phase 0, not guessed here.

PBKDF2 rather than Argon2id on purpose: it is `hashlib`, every Bitwarden client supports it, and it
keeps a second KDF implementation out of the box. 600k is the current Bitwarden default.

`user_key` exists only for the register payload. It is not sealed and not written down. Recovery
holds the password instead (§5); a later reset logs in with that password, reads the encrypted
`key` back from the account, and re-wraps it through the public password-change endpoint.

**Prove it, do not trust the 200.** After registering, `POST /identity/connect/token`
(`grant_type=password`, `username=email`, `password=mp_hash`, `scope="api offline_access"`) and
require an `access_token`. This is the same discipline as `ha_set_password`, which never believes
the CLI's exit code and logs in instead. A vault account that cannot be opened is a failure, and
the owner must be told at creation time, not by their partner a week later.

**Refuse to create a vault if `RECOVERY_BACKUP_KEY` is missing.** Invariant 8 cannot be kept on a
box that never got backup-unlock (phrase mint failed at setup, or a pre-feature install that has
not typed the phrase in Settings). The checkbox is disabled with that sentence, not silently
minting an unrecoverable vault.

### 3.5 Home Assistant — gated by a verdict that already exists

Mint an admin token the way `ha_login_works` already proves a password, then one more call:
`/auth/login_flow` → `/auth/login_flow/<flow_id>` → `/auth/token`, using `HA_ADMIN_USER` /
`HA_ADMIN_PASSWORD`. (`create_ha_admin` in `utilities.sh` is onboarding, not this.) Cache the
token in the session (§3.1). If `HA_PASSWORD_MANAGED != "true"`, the whole HA path is refused up
front with "Home Assistant manages its own login — HomeBrain cannot add people to it", which is
the same sentence rotation already uses (exit code 3).

Then one WS session on `ws://127.0.0.1:8123/api/websocket` (`websocket-client` is already a
dependency and already used at `src/app.py:3434`):

| Action | Command |
|---|---|
| create person | `config/auth/create {name, group_ids: ["system-users"], local_only: false}` → `user_id` |
| give them a login | `config/auth_provider/homeassistant/create {user_id, username: uid, password}` |
| make them a Person | `person/create {name, user_id}` — so presence automations can name them |
| reset password | `config/auth_provider/homeassistant/admin_change_password {user_id, password}` |
| remove | `config/auth_provider/homeassistant/delete {username}` then `config/auth/delete {user_id}` |

`system-users`, never `system-admin`: a member cannot add integrations, edit config, or reach the
Users page. `local_only: false` so the tunnel still works for them. Both facts belong in the
checkbox hint, along with the one that surprises people — **HA has no per-entity permissions, so a
member can control every device.**

### 3.6 Partial failure: never roll back

Order inside a request that mints a password:

1. Mint.
2. **Seal** (§5.1). If the seal fails, stop — nothing has been created.
3. Nextcloud → Vault → Home Assistant, each result reported separately.

If the vault register fails, the member still has working files, a valid password, and an escrow
entry; the row shows `files ✓ · vault ✗`, and the owner retries with "add to Vault" (§3.7), which
reuses the sealed password. Rolling back would mean deleting accounts this request did not
necessarily create — the one thing §8 forbids.

Seal-before-create is the one ordering that keeps invariant 8 without a rollback. Seal-after-vault
leaves a crash window where the account exists and the blob does not.

This falls straight out of §3.1: because the roster is derived, a partial create is not a corrupt
state, it is just a person who is on two services instead of three.

### 3.7 Two routes: add a service, or issue a new password

These are not the same act. Collapsing them into one primitive that always mints would reset
Nextcloud and HA every time the owner ticked Vault a month later. Phones with app passwords would
survive; WebDAV, browser logins, and HA would not. Today's `/pair` confirm copy is already careful
about that. Keep it careful.

**Add a service** — `POST /api/household/members/<uid>/services {services: [...]}`. Does not mint.
Unwraps the sealed password and applies it to the newly ticked services. If there is no escrow
(member created before this feature, or created by hand), the owner types the password from the
sheet once; that value is sealed and then applied. If the unwrap succeeds but the password no
longer opens a service they are already on, stop and say so — do not invent a second password in
this route.

**Issue a new password** — `POST /api/household/members/<uid>/password`. Mints, applies to every
service they are already on, re-seals, returns a new sheet and QR. This is today's `/pair`, with a
vault/HA dimension. The confirm copy stays: phones paired earlier keep working; computer logins
need the new password. If the vault probe says `not recoverable`, this route still resets files
and HA, leaves the vault alone, and says so — a silent desync is worse than a vault we cannot open.
If the probe says `unknown` (§5.5), it refuses the vault *and* reports that it could not check,
which is a different sentence: one means we tried and were told no, the other means we never got
to ask.

`POST /api/household/members/<uid>/pair` stays untouched through phase 1. **Phase 2 deletes it**
and rewires `repairMember` to `/password`; it is that route with a service dimension, and keeping an
alias would leave two ways to reset a person, one of which cannot see the vault. Decided here so it
does not linger.

### 3.8 The handover sheet

Reuse `src/static/creds_sheet.js`: add a sibling `buildMemberSheet` next to `buildCredsSheet`,
sharing `_hbSheetParts` and the filename helper, tested by the existing JavaScriptCore harness
(`scripts/tests/test_creds_sheet.js`). ASCII, CRLF, printable — same reasons as the owner's sheet.

```
HomeBrain -- Alex
Generated: 2026-09-01 14:32
Password:  correct-horse-battery-staple-copper-wren

Files   https://nc.example.house        user alex     (or scan the code)
Vault   https://vault.example.house     alex@homebrain.local
Home    https://ha.example.house        user alex

Forgot it? Ask the owner — they can issue a new one from the dashboard.
Saved vault items survive that reset, unless you changed the vault
password yourself in the Bitwarden app.
```

That last paragraph is the one people actually need, and it is only true because of §5. The
sentence it replaces — that a lost vault is lost forever — was the honest thing to say without an
escrow, and would have to go back if D5 were ever reversed.

---

## 4. Manually created accounts: stability first

Hand-made accounts stay stable. Discovery is read-only. `user:list`, `GET /admin/users` and
`config/auth/list` change nothing. A member created by hand months ago appears in the roster with
their chips and keeps their password until the owner explicitly acts on that person.

| Situation | Path |
|---|---|
| NC `alex` exists, no vault, no HA | Tick a service → `POST .../services`. No escrow, so the owner types the sheet password (or issues a new one, loud, via `/password`). |
| NC `alex` + hand-made HA login `alex` | Already matched — nothing to do. The chip just shows ✓. |
| NC `alex` + HA login named `Alex Smith` | Two rows. Honest. No rename button in v1. |
| NC `alex` + vault account under a real email | **Cannot be merged by us.** Their vault is theirs; changing its email re-keys it and only their client can do that. Listed under "not matched to a person" with that sentence. Or they change their email to `alex@homebrain.local` inside Bitwarden, and the row merges by itself on the next refresh — still not recoverable, because we never issued that password. |
| Vault or HA account with no NC user | Same unmatched list. Read-only. Includes the owner's own vault account, which is correct — it is not a household member. |

A person whose ids differ everywhere and who does not want to rename anything simply shows as more
than one row. That is honest, costs nothing, and is the alternative to a linking table that would
have to be maintained forever for a household of four.

v1 does not wrap a vault HomeBrain did not create, and does not rename HA logins. Both are real
follow-ups; neither is required to ship one password per person the dashboard created.

---

## 5. Recovery — the phrase reaches the vault too

One password means "they forgot it" is one event. It is not one repair, because the password does a
different job in each service:

| Service | What the password is there | Forgotten |
|---|---|---|
| Files | a lock, checked against a stored hash | `occ user:resetpassword` — their files were never encrypted with it |
| Home | a lock, checked against a stored hash | `admin_change_password` — nothing was encrypted with it either |
| Vault | **the key material itself** | resetting it server-side yields a working login onto unreadable ciphertext |

So Files and Home are *already* recoverable today, transitively: the phrase resets the master
password (`rotate_master_password.sh`), the master password opens the dashboard, and the dashboard
resets theirs. Nothing new is needed for those two beyond saying so — and beyond keeping Nextcloud's
server-side encryption off, which `repair_household_member` already refuses to work around.

The vault is the only real gap. It closes on one fact: **HomeBrain minted the password, so it can
hold that password once, sealed, and later log in as them.** A login plus Vaultwarden's own
password-change endpoint re-wraps `user_key` under the new password. No `UPDATE` on their table.

### 5.1 The envelope — reuse `RECOVERY_BACKUP_KEY`

`BACKUP_UNLOCK.md` §3.2 already solved the identical problem: a live box does not have the phrase at
the moment it needs to wrap something, so a phrase-derived key is persisted when the phrase *is* in
memory. That key already ships — `RECOVERY_BACKUP_KEY` / `RECOVERY_BACKUP_SALT`, written by
`build_recovery_record` (`src/recovery.py:266`). Nothing new has to be minted, stored in `.env`, or
explained to the owner.

```
escrow_key  = HKDFExpand(b64d(RECOVERY_BACKUP_KEY), info=b"homebrain-member-escrow-v1", 32)
escrow[uid] = AES-256-GCM(escrow_key, nonce, password, aad=uid)
```

Domain-separated, so the archive wrap key and the member-escrow key are different keys and neither
yields the other. The AAD binds each blob to its own uid: a swapped entry fails to open rather than
decrypting into the wrong person's login.

`/var/lib/homebrain/member_escrow.json`, mode 600 — a small, dull file:

```jsonc
{ "v": 1, "wraps": { "alex": { "nonce": "<b64 12B>", "ct": "<b64>" } } }
```

This does not contradict D4. The roster stays derived; the only thing written down is the one thing
no service can recompute.

**Delete `escrow[uid]` in the same request that deletes the member.** A reused uid must not inherit
the previous person's password. An unused wrap after a failed create that never made an account is
deleted too.

**Prune what outlives its person.** `DELETE` takes `services[]`, so a member can lose Files and keep
a vault, or a delete can half-fail. Either way the uid stops being a person on the roster while a
sealed, still-valid password stays in the file. An entry whose uid has no account in *any* of the
three services is pruned — on delete, and again whenever `check_vault` runs, which reports the
count. A blob nobody can name is a secret nobody is watching.

**One writer, atomically.** The file is small and the temptation is to `json.dump` it in place. Two
requests that both seal — adding two people quickly, or delete-then-add — are two greenlets under
gevent interleaving a read-modify-write, and the loser's entry is gone. Silently, and only
discovered on the day it is needed. Use the `update_env_var` discipline: mkstemp in the same
directory, write, `os.replace`. Serialize the read-modify-write behind one lock so the sequence,
not just the write, is atomic.

### 5.2 It must travel — and restore must re-wrap

`backup.sh:409` already dumps the vault DB and data dir, so an archive carries the ciphertext.
Without the escrow beside it, a restore onto new hardware lands exactly where `HOMEBRAIN_EMAIL_KEY`
would have — user data bound to a key that stayed behind.

Copying `member_escrow.json` next to `instance_secrets.env` is **not enough**. The blob is wrapped
with `HKDFExpand(RECOVERY_BACKUP_KEY)`, and that key is deliberately *not* in `instance_secrets.env`
(`BACKUP_UNLOCK.md` §3.2). Same-box restore with a live `.env` still opens it. New hardware does
not:

- Wizard restore that *adopts* the phrase calls `build_recovery_record(typed)`, which mints a
  **new** `RECOVERY_BACKUP_SALT` and therefore a new `RECOVERY_BACKUP_KEY`. Escrow was wrapped
  under the old key.
- Restore opened with the master password never has the old recovery key in memory at all.

So the archive also carries a restore-only wrap file, `member_escrow.wrap`, holding the
`RECOVERY_BACKUP_KEY` (or the derived `escrow_key`) that sealed the json. It lives inside the
encrypted body, the same place as `HOMEBRAIN_EMAIL_KEY`. After decrypt, `restore.sh`:

1. Unwraps every entry with that key.
2. Re-seals under the **destination** `.env`'s `RECOVERY_BACKUP_KEY`.
3. Writes `member_escrow.json` to `/var/lib/homebrain/`.
4. Discards the wrap file — it must not land on dest next to the ciphertext.

If dest has no `RECOVERY_BACKUP_KEY`, fail loudly on the escrow step. Do not write plaintext
passwords. Do not leave a wrap file behind. Do not silently drop the json and report the restore
as fine.

This is not a two-line change. It is the `HOMEBRAIN_EMAIL_KEY` lesson applied to something that
*can* be re-wrapped, so dest's phrase and salt win.

Same-box restore does not need the wrap file (`.env` still has the key). Phrase-unlock restore
could re-derive it from the header salt. The wrap file exists for the path that is actually a
dead-box restore: they remember the master password, the phrase was never typed (or was
regenerated into a new salt). Dropping escrow on that path would make “issue new passwords” true
for files and HA and **false for the vault** — the public password-change API needs the old
password, which is the whole of D5. Dual-wrapping escrow like HBK1 would cost more crypto for the
same master-path outcome, because HBK1 already opens the body. The wrap file is the envelope.

What that costs is §6.5: an archive plus either secret now yields every member login the box
issued. That is accepted, not deferred.

`member_escrow.wrap` sits in `STAGING_DIR` in the clear until tar+gpg. That is the exposure
`instance_secrets.env` already carries, but `backup.sh` holds `SEAL_DIR` to a visibly higher
standard — 0700, removed on signal, with a comment saying why. **The wrap file is held to the
`SEAL_DIR` standard, not the staging one**: 0600, and removed by the cleanup trap on every path,
not only the happy one. Unstated, it would inherit the looser rule by default.

`nuclear_reset.sh` mints a new phrase; it re-wraps escrow the same way regenerate does (§5.3).

### 5.3 Phrase regeneration re-wraps before it commits

`build_recovery_record` mints a fresh `RECOVERY_BACKUP_KEY` every time it hashes a phrase. Miss the
re-wrap and every escrow rots silently, which is the worst failure available to a recovery feature.

`POST /api/recovery/regenerate` today commits the new key to `.env` **first**, then rewraps
archives on a daemon thread, on purpose: a 500 after mint must not lose a phrase nobody has seen
(`src/app.py:4729`). Archives are large; escrow is not. The two disciplines coexist:

1. Read the old `RECOVERY_BACKUP_KEY` into memory.
2. Re-wrap every escrow entry under the new key. If any entry cannot be re-wrapped, **fail
   loudly and do not write `.env`.**
3. Then commit the new recovery record, return the phrase, and kick `_rewrap_local_archives` as
   today.

The archive thread stays after the point of no return. Escrow does not.

**The recovery *reset* path is not a re-wrap path, and that is worth saying out loud.**
`/api/recovery/*` → `rotate_master_password.sh` changes the master password and every secret derived
from it; it does not touch `RECOVERY_*`. So `RECOVERY_BACKUP_KEY` is unchanged and every seal
survives — which is the answer to the question every reader of §5 asks next. It holds only while
that flow leaves the phrase alone: if it ever retires a used phrase (`RECOVERY_SHEET.md` F7/F8),
it becomes a re-wrap path and inherits this section.

### 5.4 The repair — login, then the public API

```
old_pw      = unwrap(escrow[uid])
kdf         = POST /identity/accounts/prelogin {email}   # the account's OWN kdf, not ours
old_hash    = masterPasswordHash(old_pw, email, kdf)
resp        = POST /identity/connect/token (old_hash)    # must succeed
enc_key     = resp["Key"]                                # the protected user key, in the login body
user_key    = decstring2(enc_key, under=stretch(old_pw, kdf))
new_key     = encstring2(user_key, under=stretch(new_pw))
POST /identity/accounts/password  {
    masterPasswordHash:    old_hash,
    newMasterPasswordHash: masterPasswordHash(new_pw, email),
    key: new_key
}
# prove
POST /identity/connect/token (new hash) → access_token
re-seal new_pw under escrow[uid]
```

**No `/api/sync`.** Vaultwarden's `authenticated_response` already returns `"Key": user.akey`
beside `PrivateKey`, `Kdf`, `KdfIterations`, `KdfMemory` and `KdfParallelism` (§13). The protected
key arrives with the login, so the repair is two calls plus the proof, and "where `key` sits on the
sync payload" stops being a phase-0 question.

**`prelogin` first, always.** Everything else in this document assumes PBKDF2/600k because that is
what §3.4 registers. A member who changed their KDF in the Bitwarden app breaks that assumption,
and deriving with our defaults would fail the probe and blame their *password*. `prelogin` is
unauthenticated, cheap, and makes the refusal say the true thing: *this vault uses key settings we
did not issue*. We still do not rotate it — §12 — but we say why.

Only the path name (`/identity/accounts/password` vs `/api/accounts/password`) is left for phase 0.
The point stands: we are a Bitwarden client for this call, not a storage engine.

Every item row is untouched, because `user_key` does not change. Then prove it, as everywhere
else, before the request returns.

If the old hash does not yield a token, the vault is not recoverable. Files and HA still reset.
The owner is told the vault password in the Bitwarden app is now the one they chose, and we
cannot rotate it.

### 5.5 Two chips, because they are two different claims

Two different facts, and they must not share a word.

**`sealed`** — on the roster. "An escrow blob exists for this uid." `GET /api/household/members`
does not unwrap, does not PBKDF2, and does not log in as anyone. A member with a vault and no blob
reads as not sealed.

**`recoverable ✓`** — on the person view. We unwrapped, logged in, and it worked. This is the
check that catches a self-changed vault password, and it is the only one that may claim the word.

Calling both "recoverable" would put a state on the list that we have not verified and sometimes
know to be false — the one place this design would knowingly show a lie.

The live probe runs where a password is already in memory (create, `/services`, `/password`,
restore re-wrap, phrase regen) and when the owner opens that person
(`GET /api/household/members/<uid>`), never on the list. A house of four must not cost four 600k
KDFs every time the Household tab opens. Cache the verdict per uid for a few minutes; opening
the same person twice must not probe twice.

**A failed probe is not one state, it is three.** Vaultwarden's `check_limit_login` is keyed by
**IP alone** and returns **429** (§13). Every probe HomeBrain makes originates from `127.0.0.1`,
so the entire household shares one bucket:

| Probe result | Verdict | What `/password` does |
|---|---|---|
| token returned | `recoverable ✓` | resets the vault too |
| 401/400 — the hash is wrong | `not recoverable` | resets files and HA, refuses the vault, says why |
| 429, connection refused, timeout | **`unknown`** | **refuses to act on the vault, and says it could not check** |

Collapsing 429 into "not recoverable" would report a healthy vault as broken because someone
clicked through the list, and then have `/password` decline to fix a vault it could have fixed. The
short-TTL cache exists as much to keep the bucket intact as to save the KDF.

`check_vault` in selftest is the cheap shape: container, user count, escrow file present and
openable under the current key, and **no blob whose uid has no account in any service** (§5.1). It
does not log into member vaults.

### 5.6 What it costs, said plainly

`RECOVERY_BACKUP_KEY` sits in `.env` in plaintext, exactly as it does for archives. So the honest
sentence is not "only the phrase can open a member's vault". It is:

> **The box can log in as any member whose password it issued, for as long as they have not
> changed that password themselves. The recovery phrase — or the master password, via the wrap
> file — is what carries that ability onto new hardware after a restore.**

That narrows `VAULT_PLAN.md` §3 and the must-state UX truth at `RECOVERY_PHRASE.md:187` rather than
inverting them wholesale: vaults HomeBrain minted are recoverable until the member changes them;
vaults it did not create stay end-to-end. Both docs must be rewritten in the same PR rather than
quietly contradicted — a stale promise about who can read a vault is worse than never having made
it. It belongs in one sentence on the screen where the vault checkbox is ticked, not in a footnote.

---

## 6. The archive the escrow rides in

§5 stakes the entire recovery guarantee on the archive: a sealed password is worth exactly what the
archive carrying it is worth. So the vault's path through `backup.sh` and `restore.sh` was read end
to end rather than assumed. **F1–F6 below are pre-existing defects, not consequences of this
feature.** They are folded in here because §5 cannot be honest without them — a recovery promise
resting on a backup path that treats the vault as optional is a promise with a hole in it. Each is
independently shippable, and F2 and F4 are the two smallest changes with the most coverage.

### 6.1 What already works

Read, not run — no restore was performed for this:

| Step | Where | |
|---|---|---|
| Vaultwarden stopped for a consistent snapshot | `backup.sh:414` | ✅ |
| DB dumped — `mysqldump` of `VAULT_DB_NAME` | `backup.sh:417` | ✅ |
| Whole data dir rsync'd: `rsa_key.pem`, **attachments, sends** | `backup.sh:431` | ✅ |
| Present in all three strategies, `system` included — so the vault does go off-site | no strategy guard | ✅ |
| Restore drops, recreates and imports the DB as root | `restore.sh:484` | ✅ |
| Data dir restored with `65534` ownership | `restore.sh:481` | ✅ |
| Cert SAN re-derived for new hardware | `restore.sh:516` | ✅ |
| `VAULT_ADMIN_NONCE` deliberately not carried — the admin token re-derives from the new master | `provision_vault.sh` | ✅ |

The mechanism is sound, and §5.2's "the archive already carries the ciphertext" holds.

### 6.2 The vault is second-class in the one path where it must not be

**F1 — every vault failure is a warning; the same Nextcloud failure is fatal.**

| | Nextcloud | Vault |
|---|---|---|
| dump command fails | `die` | `log_warn` |
| dump file is empty | `die` | `log_warn` |
| data sync fails | `die` | `log_warn` |
| import fails on restore | — | `log_warn` |

For the one store whose loss is unrecoverable, that is backwards. A backup completes *successfully*
with no vault in it; a restore reports success having imported nothing.

**F2 — the cleanup trap resurrects Home Assistant, not Vaultwarden.** `backup.sh:61` restarts
`$HA_CID` on `EXIT INT TERM`, with a comment explaining exactly why. The vault is stopped the same
way at `:415` and restarted only on the happy path at `:441`. A Ctrl-C or a shutdown inside the
dump-and-rsync window — the slowest part once attachments exist — leaves the password manager
stopped, silently. One line, symmetric with the HA one.

**F3 — nothing verifies the vault came back.** Restore waits for `nextcloud` and `homeassistant` to
go healthy, re-applies proxy settings, and re-syncs the HA password — a step that exists *because*
the self-test caught a restored box lying about HA. The vault gets `refresh_vault_lan_ip`, no health
wait, no row count, no login probe.

**F4 — `selftest.py` and `healthcheck.py` have no vault coverage at all.** The self-test checks the
dashboard, Nextcloud and HA passwords, Nextcloud data, local backup, off-site, instance secrets,
remote access and version. Not the vault — not its container, not its user count, not whether it
answers. Which makes F1 and F3 unobservable: nothing in the product will ever tell the owner the
vault came back empty, while `DISASTER_RECOVERY.md:108` promises "Nextcloud, Home Assistant,
Vaultwarden and the AI agent's workspace come back as they were."

The fixes are small and independent: promote the vault's dump and import failures to `die` when the
vault is enabled and running; add the trap line; `wait_for_healthy vaultwarden` plus a user-count
assertion after restore; a `check_vault` in `selftest.py` sitting next to `check_ha_password`.

### 6.3 Two smaller mismatches

**F5 — `data_only` silently carries the whole vault.** The strategy header reads
`data_only — NC data + HA config (no DB, no NC config)`, but the vault block has no strategy guard.
Not harmful, arguably right — but anyone deciding from that comment which archives hold secrets gets
it wrong. Correcting the comment is the honest fix; guarding the block would be the wrong one.

**F6 — an archive with no vault leaves the live vault untouched.** `HAS_VAULT_DB=false` skips the
block, so Nextcloud and Home Assistant roll back to the archive while the vault stays at *now*.
Defensible — do not destroy what the archive does not contain — but undocumented, and it produces a
box whose services come from two different points in time.

### 6.4 Escrow and the vault DB are one unit

F6 stops being cosmetic here. Restore `member_escrow.json` while `HAS_VAULT_DB=false` and the sealed
passwords describe the archive's vault while the live vault is at *now* — so `recoverable ✓` probes
today's vault with an archived password and reports whatever it happens to find. The reverse is
worse: a vault restored without its escrow silently loses recovery for every member in it.

**Rule: the escrow is restored if and only if the vault DB is restored.** If one is present and the
other is not, restore neither, log it, and surface it on the Backup page. They are one unit, or they
are skew.

### 6.5 What an archive is now worth

`RECOVERY_BACKUP_KEY` lives in `.env`, and `.env` is not in the archive — only
`instance_secrets.env`, carrying two keys, neither of them that one. That used to mean a
master password opened files, HA, and vault items as ciphertext, and not a member login.
`member_escrow.wrap` places the sealing key inside the encrypted body, and the HBK1 header wraps
one DEK twice — once to the master password, once to the recovery key. Anything in the body is
reachable from either side:

| You hold | Before §5 | With `member_escrow.wrap` in the body |
|---|---|---|
| archive + master password | files, HA config, vault items as ciphertext | **+ every member's password** |
| archive + recovery phrase | the same | **+ every member's password** |

And the escrow holds a member's *single* password, so that is not merely their vault: it is their
files and their Home Assistant login in the same blob. Member vault *items*, which an archive
could not open before, become readable.

That is the price of D5 surviving a master-password restore. Dropping the wrap file would keep
the old property and gut the restore path that uses the secret they actually remember: without
escrow, the dashboard cannot rotate a member vault through the public API, and “issue new
passwords” would leave those vaults as ciphertext nobody can log into. The wrap file stays.
The off-site copy is the place that matters — it is the one an attacker can reach without the
box.

---

## 7. Surfaces after the change

### New

| File | Purpose | Size |
|---|---|---|
| `src/vault_account.py` | Bitwarden key derivation, EncString, register + password-change payloads. Pure. | ~120 |
| `scripts/tests/test_vault_account.py` | Vectors + round-trips + Expand-not-Extract | ~90 |
| `src/household.py` | `merge_roster`, HA token + WS helpers. Flask-free except the token cache lives with the session in `app.py`. | ~150 |
| `scripts/tests/test_household_roster.py` | `merge_roster` + guards | ~120 |
| `src/member_escrow.py` | Seal / open / re-wrap `member_escrow.json`. Pure. | ~70 |
| `scripts/tests/test_member_escrow.py` | Round-trip, AAD binding, re-wrap, concurrent seals, pruning | ~110 |
| `scripts/tests/test_vault_probe.py` | 429/401/timeout → `unknown` / `not recoverable` verdicts | ~50 |

Do not grow the household section of `app.py` by another few hundred lines. Routes stay there and
stay thin.

### Changed

| File | Change |
|---|---|
| `src/app.py` §household (`:4742`–`:5117`) | Thin routes; call `household.py` / `vault_account.py` / `member_escrow.py` |
| `src/app.py` `recovery_regenerate` | Re-wrap escrow **before** committing the new `RECOVERY_BACKUP_KEY` (§5.3) |
| `src/static/dashboard.js` (`:1609`–`:1810`) | Service chips, checkboxes default off, member sheet, unmatched list, add-service vs reset |
| `src/templates/dashboard.html` (`:170`, Household tab) | Card copy (the "nobody else" paragraph), service checkboxes |
| `src/static/creds_sheet.js` | `buildMemberSheet` |
| `scripts/backup.sh`, `scripts/restore.sh` | Carry `member_escrow.json` + `member_escrow.wrap`; dest re-wraps (§5.2); escrow and vault DB restore as one unit (§6.4) |
| `scripts/backup.sh` (`:61`, `:417`, `:431`) | Restart the vault in the cleanup trap; vault dump failures become fatal (F1, F2) |
| `scripts/restore.sh` (after `:508`) | `wait_for_healthy vaultwarden` + a user-count assertion (F3) |
| `src/selftest.py` | `check_vault` beside `check_ha_password` — container, user count, escrow file openable under the current key. No member login. (F4) |
| `scripts/nuclear_reset.sh` | Re-wrap escrow when the new phrase is minted |
| `docs/TESTING.md`, `docs/DISASTER_RECOVERY.md` | E2E; what the phrase now reaches; restore re-wrap |
| `docs/plans/VAULT_PLAN.md` §3, `docs/plans/RECOVERY_PHRASE.md:187` | Narrow the E2E promise for vaults HomeBrain minted (§5.6) |
| `README.md` | The single-password line now covers everyone the owner added |

### Routes

| Route | Change |
|---|---|
| `GET /api/household/members` | Per-service chips, `unmatched`, per-service `errors`. `sealed` chip = blob exists. Does not unwrap, never claims `recoverable`. |
| `POST /api/household/members` | Takes `services[]` (default files only); per-service results; returns the sheet |
| `POST /api/household/members/<uid>/services` | **New** — add vault and/or HA; reuses escrow (or a typed password); does not mint |
| `POST /api/household/members/<uid>/password` | **New** in phase 2 — mint, apply to services they are already on, re-seal, sheet |
| `DELETE /api/household/members/<uid>` | Takes `services[]`, default all present; drops `escrow[uid]` when the person is gone |
| `GET /api/household/members/<uid>` | Live probe → `recoverable` / `not recoverable` / `unknown` (§5.5), short-TTL cached, in addition to quota/devices |
| `GET /api/household/members/<uid>/quota`, `/devices/<id>` | Untouched |
| `POST /api/household/members/<uid>/pair` | **Untouched in phase 1, deleted in phase 2** — `/password` is that route plus the service dimension (§3.7) |

### Untouched, deliberately

`rotate_master_password.sh` — a member's password is independent of the master's, and rotating one
must never touch the other. The three account stores themselves (`backup.sh` already dumps the
Nextcloud DB, the vault DB and HA's `.storage`), the dashboard login, and the agent.

---

## 8. Invariants (must hold before merge)

1. A member is never in Nextcloud's `admin` group, never `system-admin` in HA, and never gets the
   dashboard. The dashboard keeps exactly one login.
2. `pairing_payload` still refuses `NEXTCLOUD_ADMIN_USER` (#167). No new route may mint a credential
   for the owner's account.
3. HomeBrain writes to a service only for the person and service the owner just clicked. Discovery
   never writes. Adding a service never mints a password. Failure never rolls back.
4. Every vault account HomeBrain creates is proved by logging into it before the request returns.
5. A member's password reaches disk only as its own sealed blob. It does not reach `.env`, a log
   line, or `ps` — the register payload and the HA WS frame carry it in memory, and nothing else
   does.
6. The HA path refuses to run at all when `HA_PASSWORD_MANAGED != "true"`.
7. Deleting a member's vault requires the existing typed confirmation and says, in the dialog, that
   their vault contents are gone forever. Deletion is still the one irreversible act — §5 makes a
   forgotten password recoverable, not a deleted account.
8. An escrow entry is written before any vault account is created, or the vault account is not
   created at all. Creating a vault requires `RECOVERY_BACKUP_KEY`. The roster claims only
   `sealed`; `recoverable ✓` is claimed only by a probe that returned a token. A probe that
   cannot reach a verdict — 429, refused, timeout — is `unknown`, never `not recoverable`, and
   `/password` refuses the vault on both while saying which one it hit (§5.5).
9. Regenerating the recovery phrase re-wraps every escrow **before** it commits the new
   `RECOVERY_BACKUP_KEY`, and fails loudly if any entry cannot be re-wrapped. Archive rewrap stays
   after the commit, as today.
10. The unwrapped password exists only inside the request that uses it. It is never logged, never
    returned to the browser after the handover response, and never written anywhere but its own
    sealed blob. `GET /api/household/members` does not unwrap. `member_escrow.wrap` is a
    restore-staging artifact and does not remain on dest.
11. `GET /api/household/members` never fails the whole card because one service is down.
12. A backup that cannot capture an enabled, running vault fails. It does not publish an archive
    that silently lacks one (F1).
13. Escrow and vault DB restore together or not at all, and a restore that drops the escrow says
    so rather than reporting success (§6.4).
14. Every write to `member_escrow.json` is an atomic replace behind a lock that covers the read as
    well as the write. Two concurrent seals never lose an entry.
15. No escrow entry outlives its person. An entry whose uid has no account in any service is
    pruned, and `check_vault` counts what it pruned.

---

## 9. Test plan

**Off-hardware (CI, `scripts/tests/`)**

- `test_backup_vault_fatal.sh`: a vault dump that fails, or lands empty, aborts the backup rather
  than publishing an archive without one; and a signal during the vault window leaves the
  container running (F1, F2). Both are `backup.sh` shape tests in the style of
  `test_backup_retention.sh`, not live-vault tests.

- `test_vault_account.py`: a pinned golden vector (email + password → `masterPasswordHash`, captured
  once from a real Bitwarden client during the spike and frozen as a regression pin); round-trip —
  decrypt `key` with the stretched key and get `user_key` back; decrypt `encryptedPrivateKey` with
  `user_key` and load it as a valid RSA key; EncString is `2.<b64>|<b64>|<b64>` with a 16-byte iv;
  the email is lowercased before use as salt; **a payload built with `HKDF` (Extract-then-Expand)
  does not match the vector.**
- `test_household_roster.py`: `merge_roster` matches across services, excludes the owner and
  `RESERVED_MEMBERS`, reports unmatched vault/HA accounts, and never merges two different people.
  Plus the §8 guards: reserved ids, admin-group refusal, HA group is always `system-users`.
- `test_member_escrow.py`: seal → open round-trip; a blob sealed for `alex` refuses to open under
  `sam`'s AAD; re-wrapping under a new `RECOVERY_BACKUP_KEY` preserves every entry and invalidates
  the old key; a truncated or absent file degrades to "not sealed", never to a crash;
  restore-style re-wrap (open under key A, seal under key B) leaves nothing openable with A;
  **two concurrent seals both survive** (invariant 14 — the one that fails on a naive
  `json.dump`); an entry for a uid absent from every service is pruned (invariant 15).
- `test_vault_probe.py`: a 429 from `connect/token` yields `unknown`, not `not recoverable`, and
  `/password` refuses the vault with the "could not check" sentence rather than the "we asked and
  were told no" one (§5.5). A 401 yields `not recoverable`. Pure verdict-mapping, no live vault.
- Existing `test_photo_pairing.py` keeps passing unchanged.

**On hardware (`homebraintest.local`, then `.58`)** — the acceptance criterion is one sentence:
*the same string opens all three.*

1. Add a member with all three services ticked. Then, with **one** password: WebDAV `PROPFIND` as
   them; `POST /identity/connect/token` returns an access token; `/auth/login_flow` returns
   `create_entry`.
2. Add a member with files only, then `POST .../services` for vault. The Nextcloud password does
   **not** change. The same string now opens files and vault.
3. `POST .../password` → all current services accept the new one; a previously paired phone still
   syncs.
4. Create a Nextcloud user by hand with a password the box does not know → it appears in the roster,
   and nothing HomeBrain does changes it until the owner clicks.
5. Delete → gone from the ticked services and from `member_escrow.json`; a reused uid does not
   inherit the old wrap.
6. Reboot; confirm the member's password still opens all three (nothing depended on request state).
7. **The recovery run.** Forget the password deliberately. Save an item in the member's vault, then
   reset from the dashboard: the new password logs in *and the item is still readable*. That last
   clause is the whole feature — a reset that returns an empty vault is the failure this exists to
   prevent, and it looks identical to success from the login screen.
8. Change the vault password in the Bitwarden web vault. Opening that person shows
   `recoverable ✗`. `/password` resets files (and HA if present) and refuses the vault, with that
   sentence. The roster still shows `sealed`, which is true and is why it does not say
   `recoverable`.
9. Open four people in a row, quickly. No verdict flips to `not recoverable` because of the shared
   login bucket, and no vault is refused a reset it could have had (§5.5).
10. Regenerate the recovery phrase, then repeat 7.
11. **Restore onto a fresh box**, wizard path, phrase-unlock (this mints a new `RECOVERY_BACKUP_KEY`
    on dest). Repeat 7 from *that* box. A same-`.env` restore does not prove §5.2.
12. Repeat 11 with a **master-password** unlock on a fresh box — that is the path the wrap file
    exists for. Confirm no `member_escrow.wrap` remains on dest afterwards (invariant 10).
13. Restore an archive that carries escrow but **no vault DB**. Neither is restored, the log says
    which, and the Backup page says it too (invariant 13).

Measure PBKDF2 600k + RSA-2048 on the Pi during phase 2, not only on x86. This runs in a gevent
worker next to `occ`; HomeCloud is a supported vault target.

---

## 10. Phasing

| Phase | Content | Exit |
|---|---|---|
| 0 — spike | Register one vault account against the pinned image by hand; confirm field names; confirm `HKDFExpand` (not `HKDF`); confirm the password-change path; capture the golden vector. **Plus an HA leg**: does `config/auth/list` return the credential username the roster joins on (§3.1), and do `config/auth/create` + `person/create` accept the shapes in §3.5? | A `curl` transcript that ends in an access token, a second that changes the password through the public API and logs in with the new hash, and a WS transcript that lists a user and reads back its username |
| 1 — roster | `merge_roster`, chips, unmatched list, per-service errors. Still Nextcloud-only. `/pair` stays. | The list shows what each person has; nothing regressed |
| 1b — archive | F1–F5: fatal vault failures, the trap line, restore verification, `check_vault`, the `data_only` comment. Ships alone, needs nothing from this feature. | A backup that loses the vault fails; a restore that loses it says so |
| 2 — vault + escrow | `vault_account.py`, `member_escrow.py`, invite + register + prove-by-login, checkbox default off, gated on `RECOVERY_BACKUP_KEY`, `/services` and `/password` (and `/pair` deleted), locked atomic escrow writes, pruning, the three-way probe verdict, delete drops the wrap, regenerate re-wraps before commit, `member_escrow.wrap` + dest re-wrap | One password opens files and vault; a forgotten password is reset and the items are still there — including after a **fresh-box** restore with either secret |
| 3 — home | HA token mint (session-cached), WS helper, checkbox gated on `HA_PASSWORD_MANAGED` and default off, person entity | One password opens all three |
| 4 — copy | Dashboard paragraph, `VAULT_PLAN.md` §3, `RECOVERY_PHRASE.md:187`, `README.md`, `TESTING.md`, `DISASTER_RECOVERY.md` | The docs agree with §5.6 |

Each phase is a shippable PR. Phase 1 alone is worth merging even if 2 and 3 slip, and **1b is
worth merging even if the rest of this document is never built** — it is six small fixes to a
path the box already depends on. It is also a precondition for phase 2: sealing passwords into an
archive whose vault failures are warnings would build the guarantee on the one part of the system
that does not check its own work. **Escrow is not
optional relative to vault**: shipping vault accounts without the sealed password would mint
unrecoverable vaults that no later change can retrofit, because the password is shown once. If
escrow cannot ship with vault, ship neither.

---

## 11. Risks

- **A Vaultwarden bump changes the register or password-change contract.** Neither is a documented
  public API. Mitigation: both live in `vault_account.py`, the E2E fails loudly, and
  `config/versions.json` bumps already require a hardware run. Note them in `docs/TESTING.md` next
  to the vaultwarden tag.
- **`HKDF` vs `HKDFExpand`.** The silent variant of the row above: register returns 200, login
  fails, items are unreadable. Phase 0 pins Expand-only against a real client vector.
- **HA WS command names are config-integration internals.** Same shape of risk, same mitigation; a
  failure is a clean per-service error, never a half-created member. Phase 0's HA leg also settles
  whether `config/auth/list` gives the roster a join key at all.
- **One login bucket for the whole house.** `check_limit_login` is keyed by IP, and every probe
  comes from `127.0.0.1`. Left unhandled this does not fail loudly — it quietly downgrades healthy
  vaults and then declines to reset them. The three-way verdict and the short-TTL cache (§5.5) are
  the mitigation; the shape test is cheap and belongs in CI.
- **The box can log in as every member it still has a sealed password for.** The price of D5, and
  the one item here that is a deliberate trade rather than a hazard. It has to be said where the
  vault checkbox is ticked, and `VAULT_PLAN.md` §3 and `RECOVERY_PHRASE.md:187` have to stop saying
  the opposite.
- **An escrow that quietly stops working.** The failure mode with teeth: nobody notices until the
  day it is needed. Three guards — write paths and the person view prove with `connect/token`
  (§5.5), regeneration re-wraps or fails loudly *before* `.env` commit, and the hardware test
  restores onto a **fresh** box (phrase *and* master-password) and recovers from *that* box.
- **Restore that copies ciphertext and not a wrap key.** Same class of bug as forgetting
  `HOMEBRAIN_EMAIL_KEY`. Mitigation is §5.2, not a comment in `backup.sh`.
- **The archive path treats the vault as optional (F1–F4).** The recovery guarantee inherits every
  weakness of the thing carrying it, and today that path can lose the vault in four places without
  saying a word. Phase 1b, before phase 2.
- **The escrow makes the archive worth more than it was.** §6.5. An archive plus either secret
  now yields working logins for everyone in the house, where before member vaults were ciphertext.
  The off-site copy is the place that matters — it is the one an attacker can reach without the
  box.
- **CPU inside a gevent worker.** PBKDF2 600k + RSA-2048 is small on x86 next to `occ`, and not
  obviously so on a Pi. Measure in phase 2.
- **Whole-house control for HA members.** A product risk, not a bug. It is why D2 defaults to off.

---

## 12. Out of scope

- **Wrapping a vault HomeBrain did not create** ("Protect this vault"). A new route and a password
  prompt, and it would extend D5 to the owner's vault if they clicked it. Follow-up, if anyone asks.
- **Renaming a mismatched HA login.** One WS call (`admin_change_username`); not needed to ship.
- **Recovering a vault after the member changed its password in the Bitwarden app.** That is the
  `user_key` + SQL design we rejected. The chip tells the truth; we do not touch that vault.
- **Bitwarden organizations / shared collections.** The obvious next ask ("the Netflix login for
  everyone") and a genuinely bigger feature: org key, per-member org keys, collection ACLs. Each
  person gets their own vault first.
- SSO across the three services. `INTEGRATIONS_PLAN.md:310` still holds.
- Email invitations, SMTP, password hints.
- Members reaching the dashboard, the agent, or OpenClaw. `INBOUND_AGENT_CONTENT.md:24` holds:
  there is one agent and it is the owner's.
- FTP accounts — a separate card with a separate purpose (cameras, not people).
- Renaming a member's Nextcloud uid. `occ` cannot, so neither can we.

---

## 13. Sources

Verified against the pinned images, not from memory:

- Vaultwarden 1.36.0 `src/api/identity.rs` — `#[post("/accounts/register")]` is mounted; also
  `/accounts/prelogin` and `/connect/token`. Password-change path confirmed in phase 0.
  `authenticated_response` returns `"Key": user.akey` with `PrivateKey`, `Kdf`, `KdfIterations`,
  `KdfMemory`, `KdfParallelism` — so the protected key comes back with the login and §5.4 needs no
  `/api/sync`.
- Vaultwarden 1.36.0 `src/ratelimit.rs` — `check_limit_login(ip)` is keyed by **IP only**
  (`LIMITER_LOGIN.check_key(ip)`) and answers `429 "Too many login requests"`, governed by
  `login_ratelimit_seconds` / `login_ratelimit_max_burst`. Every probe from this box shares one
  bucket, which is why §5.5 has three verdicts and not two.
- Vaultwarden 1.36.0 `src/api/core/accounts.rs` — `_register` admits an email when
  `Invitation::take(&email, &conn)` succeeds, *before* consulting `CONFIG.is_signup_allowed`. This is
  what makes provisioning work with signups locked down.
- Vaultwarden 1.36.0 `src/api/admin.rs` — `POST /admin/invite {email}`, `GET /admin/users`,
  `GET /admin/users/by-mail/<mail>`, `POST /admin/users/<id>/delete`.
- Home Assistant `components/config/auth_provider_homeassistant.py` — `config/auth_provider/
  homeassistant/{create,delete,admin_change_password,admin_change_username}`, all `@require_admin`.
- Home Assistant `components/config/auth.py` — `config/auth/{list,create,delete,update}`; `create`
  takes `name`, optional `group_ids`, `local_only`.
- `ha_login_works` `scripts/common.sh:1086` — `/auth/login_flow` → `/auth/login_flow/<id>` until
  `create_entry`. Member provisioning adds `/auth/token` and caches the result.
- Vaultwarden 1.36.0 `src/db/models/user.rs` — `CLIENT_KDF_ITER_DEFAULT = 600_000`, which is where
  §3.4's iteration count comes from. We do not write this table.
- Vaultwarden 1.36.0 `src/api/core/emergency_access.rs` — the road not taken: invite/accept/
  confirm/initiate/takeover plus `POST /emergency-access/<id>/password`. It is the vendor's answer
  and needs no DB write, but only the grantor can approve instantly — and the grantor is the person
  who lost their password, so recovery would wait for `waitTimeDays` to elapse in a background job.
- `scripts/backup.sh:409` — the vault DB and data dir are already in every archive. Escrow
  carriage is on top of that, and restore re-wraps (§5.2).
- `src/app.py:4729` — regenerate commits `.env` before archive rewrap. Escrow rewrap happens
  before that commit (§5.3).
- `docker-compose.yml` — `INVITATIONS_ALLOWED=true`, `SIGNUPS_VERIFY=false`, vaultwarden on
  `127.0.0.1:${VAULT_PORT}`. No change needed.
- `scripts/backup.sh:55-81` (cleanup trap, strategies), `:409-442` (the vault block),
  `:205-237` (`instance_secrets.env` and its reasoning) — the basis for §6.1 to §6.3.
- `scripts/restore.sh:227-243` (`HAS_VAULT_*`), `:475-508` (vault restore), `:516` onward
  (restart, proxy re-config, HA password sync — and no vault equivalent).
- `src/selftest.py` — the check list, which has no vault entry: `check_dashboard_password`,
  `check_nextcloud_password`, `check_ha_password`, `check_nextcloud_data`, `check_local_backup`,
  `check_offsite`, `check_instance_secrets`, `check_remote_access`, `check_version`.
