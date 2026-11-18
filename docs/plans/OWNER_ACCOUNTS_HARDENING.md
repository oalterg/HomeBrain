# Owner accounts and account maintenance

Status: proposed implementation plan. No production changes are authorized by this document.

## Outcome and decisions

A fresh HomeBrain installation gives its owner a usable account in every enabled service,
including a Vaultwarden vault. Household shows one owner with the actual service identities,
alongside additional household members. Existing accounts and their contents survive upgrades.

Decisions for this plan:

- Keep the single-owner model. Do not require a second Oliver account just to align services.
- For new installations, retain the existing Nextcloud and Home Assistant `admin` defaults.
  Provision a personal Vaultwarden account as `admin@homebrain.local` when the configured
  Nextcloud owner is `admin`; use the existing stable synthetic-email convention otherwise.
  Keep the default owner labeled `admin` in Household, with an Owner badge. Do not add owner
  naming or display-name editing. Vault login still requires the full email address.
- The managed owner vault initially uses the HomeBrain master password. Its server-admin token
  remains separate and internal. Rotation, recovery, and restore are release requirements for
  this promise, not follow-up work.
- Adopted vault accounts retain their email, encryption settings, and contents. Linking an
  account does not imply permission to change its password or email.
- Preserve the current prohibition on HomeBrain phone QR pairing for the owner/admin account,
  as requested. Document that restriction in the owner card and setup handover. Additional
  non-admin household accounts remain the supported QR-pairing path.
- No automatic account deletion, merging, or renaming on upgrade. No production repair as a
  side effect of shipping this feature. No new unit tests; verify with service integration and
  end-to-end scenarios against disposable installations.

## Evidence from the production cleanup

The cleanup on 2026-11-16 exposed these concrete failures:

1. Household listed Home Assistant's only owner as unmatched because its username differed
   from the Nextcloud owner. The personal vault was also unmatched. Neither label established
   that an account was unnecessary.
2. `scripts/provision_vault.sh` provisions server administration and storage, but no personal
   vault. `vault_bootstrap()` in `src/app.py` only invites the first user to finish registration.
   `_create_vault_account()` already implements registration and login verification for members.
3. Repeated independent admin logins hit Vaultwarden's rate limit. `_vault_admin_jwt()` caches
   its token in a manager session and loses the distinction between throttling and other errors.
4. Nextcloud's installed ownership-transfer command moved files and shares, but left file
   versions and trash behind. Passwords required `passwords:user:move`. The old HomeBrain
   Nextcloud integration also needed removal. The source had 69,331 live files, 6 shares,
   10 Passwords entries, 2,624 version files, and 294 top-level trash records.
5. The Bitwarden CLI logout invocation returned 127. The root cause was not established;
   distinguish a missing executable, a broken executable, and the service user's PATH before
   proposing a fix. Removing a saved session is not evidence of successful CLI logout.

Further code findings:

- `rotate_master_password.sh` rotates the Vaultwarden administration token, not a personal vault.
- `selftest.check_vault()` can report success with zero vault users; this proves service liveness,
  not owner readiness.
- `dashboard.js` gates first-vault setup on public signups being open. Zero users with signups
  closed can therefore hide the setup action, although admin invitations are the appropriate path.
- `pairing_payload()` and `activation.py` deliberately exclude owner phone pairing. Retain that
  boundary while correcting owner-account and setup copy.

## 1. Owner identity and Household

Keep the roster derived from live services. Add only the owner linkage that cannot be inferred
reliably; do not introduce a second database of household membership.

- Nextcloud: use `NEXTCLOUD_ADMIN_USER`, resolving comparisons with Nextcloud's identity semantics.
- Home Assistant: discover the live `is_owner` user and its immutable ID. Compare it with
  `HA_ADMIN_USER`; surface discrepancies without silently modifying authentication. The service
  owner and the configured integration administrator may differ on an adopted installation.
- Vaultwarden: persist the explicitly linked owner UUID, email, and whether HomeBrain manages
  its password in the existing configuration mechanism. UUID is the binding; email is verified
  against the live account. Never infer ownership solely because there is one vault user or its
  display name resembles the owner.

Extend the household response with an `owner` object and per-service status. Keep `members` and
`unmatched` for their existing purposes. An owner may legitimately have different login names
across services. Missing, unavailable, unlinked, and ready are different states.

Render the existing owner as the first row in the Household list: `admin — Owner` for the
standard installation, with Files, Vault, and Home status and actual login identifiers in its
details. For installations with a different configured owner login, show that login rather than
inventing an `admin` account. Use the owner login as the row label, not a separately editable
personal name. This is a view of the existing account; do not create a duplicate household user
or change service profile names.

The owner counts as a person in the displayed household total. A single-user installation must
show `admin — Owner` and a total of one, rather than an empty list or “No household members.”
Additional members appear below it. Keep the API's owner/member distinction so rendering the
owner does not enable member deletion, password-reset, or phone-pairing actions for that row.
If an owner service cannot be queried, retain the configured owner row and show that service as
unknown. A missing or unlinked vault is visible on that row, not a reason to hide the owner.

Replace any implication of “stray” with “Accounts not linked to a household person.” Show
discovered owner/admin roles even before linkage is complete.

Protect owner accounts on the server, including direct requests to member endpoints. Do not
reuse member deletion or reset routes by weakening `not_a_member()`. Discover live HA ownership
before deletion; reject deletion when ownership cannot be determined. A supported HA rename
uses its public API, preserves the owner ID and password, updates HomeBrain only after verifying
the new login, and reports a repairable partial result if configuration persistence fails.

Existing installations get explicit choices to link an existing vault or create one if absent.
An unrelated email can be linked without re-keying or migrating it. If the user chooses password
management, prove the existing credential first and retain the account's actual KDF settings.
Unsupported KDF settings allow linkage but block managed password changes with a clear reason.

## 2. Reliable Vaultwarden administration and CLI operations

Use one server-side admin-session cache shared by Gunicorn workers. A small protected runtime
file under `/run/homebrain`, atomic writes, and a bounded cross-process lock are sufficient;
reuse existing locking patterns rather than introducing infrastructure. Store no token in API
responses or browser-session state. All callers still require their normal authorization.

- Cache until the actual upstream expiry with a safety margin. Concurrent misses produce one
  login attempt. Invalidate on credential changes and authentication rejection.
- On 401/403, refresh once. On 429, honor `Retry-After` when supplied, otherwise use a bounded
  cooldown. Share the cooldown across workers and do not repeatedly refresh during it.
- Preserve typed results for throttling, rejected credentials, connection failure, and malformed
  responses. Household must show unknown status during failures, never an empty account list.
- Do not restart Vaultwarden or weaken its rate limits to recover from ordinary polling.
- Route bootstrap, roster, proxy, provisioning, and maintenance operations through the same
  authentication helper so one path cannot defeat the cache.

Reproduce the CLI failure under the manager's actual service environment and the `homebrain`
OS user. Resolve the installed executable consistently; verify executable availability and
execution before invoking it. Lock/logout endpoints must inspect return codes, remove session
material appropriately, and report the difference between local session removal and successful
CLI logout. Include a targeted health result for the CLI only when its integration is in use.

## 3. Managed owner-vault lifecycle and first deployment

### Provisioning

Run owner-vault provisioning after Vaultwarden is healthy and before setup claims the vault is
ready. Use the existing registration crypto and invite/register/login sequence. Keep public
signups closed; owner provisioning must not depend on enabling public registration.

Serialize provisioning and make retry behavior depend on live account state. Before registering,
check the bound UUID and intended email. A collision must not trigger reset, deletion, or an
assumption that an existing account belongs to this owner. If a previous attempt registered but
did not finish recording its result, verify the intended credentials and finish the same account.

For managed vault credentials, reuse the existing recovery-backed escrow format keyed by the
canonical owner UID, while verifying the bound vault UUID before any password operation. Include
the owner in pruning logic; a linked non-synthetic email must not cause its escrow to be pruned.
Do not duplicate the master password in a new plaintext field.

Fresh setup already attempts to create `RECOVERY_BACKUP_KEY`. If that step failed, show owner
vault setup as incomplete and repair recovery setup before promising managed vault recovery.
Do not silently claim the owner vault is recoverable. Persist the sealed credential and pending
provisioning state before a remote mutation can leave a created account untracked.

Verify the login and encrypted vault-key handling, then mark the owner vault ready. If a service
fails, retain successful services and offer retry for the missing step. The handover sheet shows
the exact per-service logins and statuses, including the vault email. A disabled vault is skipped.

### Password change, recovery, and restore

Add the owner vault to both ordinary master-password changes and recovery-phrase resets using
the existing client-side crypto and authenticated Vaultwarden password-change API. Preserve the
vault's encryption key and items. An administration token cannot substitute for the old vault
credential. Verify the new login and that the protected vault key unwraps correctly before
committing the new active escrow value.

Cross-service rotation is not transactional. Use a protected, recoverable checkpoint containing
sealed old/new owner-vault credentials before changing the remote vault, and record completed
steps. On restart or ambiguous response, probe the expected credentials with bounded attempts
and resume the confirmed state. Never report “all services use the new password” after partial
failure, or discard the credential that still opens the vault. Integrate this with the existing
rotation result rather than inventing a generic job system.

If the owner changed their vault password directly, report that it is independently managed;
do not reset or recreate it. Relinking requires a working vault credential. Preserve equivalent
behavior for adopted accounts that intentionally keep their own password.

Back up the owner binding, management status, vault database/data, and escrow as a consistent
set. Extend the existing encrypted archive and escrow rewrap flow; exclude runtime JWT caches.
Restore linkage and escrow before reconciliation and never create an empty replacement vault
ahead of restoring the real one. Checkpoint writes and backup/restore must be coordinated so a
snapshot cannot pair new vault credentials with stale escrow. Restore verifies account access
and decryption, not merely container health. Older archives without owner metadata stay unlinked
and get an adoption action rather than guessed ownership.

### Readiness and copy

Separate “Vaultwarden is running” from “Your vault is ready.” Ordinary health polling should use
cheap readiness checks and cached verification status; do not perform expensive login/decryption
probes on every roster refresh. Run full verification on provisioning, explicit repair, rotation,
and restore. Surface zero-user and unlinked-owner cases even when signups are closed.

Update setup handover, credential sheets, Household, Connections, recovery, and rotation copy
together. Managed owner vaults can participate in the one-password promise; independently managed
vaults must display their exception. Explain the retained admin phone-QR restriction where pairing
is offered, without presenting it as a missing or broken owner account.

## 4. Verified Nextcloud migration, CLI first

Provide a local administrative command for the actual maintenance operation performed here.
Avoid a new migration dashboard or a general cross-service account-merging framework in v1.

1. **Inspect:** enumerate source/destination accounts, app versions, live files, versions, trash,
   incoming/outgoing shares, Passwords data, calendars, contacts, and other user-owned app data.
   Report destination conflicts, storage requirements, active clients, and unsupported data.
   Do not dump secrets or item contents into the report.
2. **Prepare:** produce an exact operation manifest. Support the normal transfer folder and an
   explicit destination-root mode. Replacing destination files is a separately explicit choice;
   it does not authorize overwriting destination passwords or other app data. Pause writes from
   affected accounts and background work, capture a consistent database/configuration/file
   recovery point, and verify it. Resume unaffected operation as soon as safe.
3. **Transfer:** use supported commands/APIs, including `files:transfer-ownership` and
   `passwords:user:move` where available. Inventory versions and trash independently and verify
   that their contents and metadata moved. Move calendars/contacts with collision handling.
   Passwords destination conflicts block v1 rather than invoke the command's destructive
   overwrite behavior implicitly. Do not transplant authentication tokens or MFA secrets.
4. **Verify:** compare paths, counts, bytes, and content checksums; verify shares, versions, trash
   restore behavior, calendar/contact contents, and decrypted Passwords data. Counts alone are
   insufficient. Read transferred content through the destination account's actual APIs.
5. **Reconcile and retire:** verify the destination HomeBrain integration, then remove the obsolete
   source integration. Only an explicitly requested retirement may delete the source, and only
   after every required verification passes. Otherwise retain it disabled and report outstanding
   work. Return client re-login instructions, backup location, and a machine-readable result.

Use tested adapters for supported installed versions. Where an upstream API does not cover a
data type, require a narrowly versioned adapter and restore E2E coverage before claiming support;
do not turn the one-off database manipulations from this incident into generic SQL substitutions.
Unknown app-owned data blocks destructive retirement. Preserve user-authored Talk content if
present, or report it as unsupported; do not assume every installation only has sample messages.

Keep a durable operation journal and exclusive lock. Resume interrupted work by checking live
state against completed steps, never by blindly replaying moves. Do not allow concurrent restore,
rotation, deletion, or another migration of affected accounts. Keep recovery points outside the
source home directory and outside routine retention until the operation is resolved. Failure
must not leave enabled/disabled or maintenance state unexplained. Document recovery procedures;
do not automatically roll back a whole live database after unrelated writes have resumed.

Vaultwarden account merging and automatic transfer between Vaultwarden and Nextcloud Passwords
are explicitly outside this migration command. They are different stores with different keys.

## 5. Delivery sequence

| Change | Deliverable | Release gate |
|---|---|---|
| 1 | Shared Vault admin authentication, precise errors, CLI failure handling | Concurrent roster reads do not flood login; lock/logout failures are truthful |
| 2 | Owner linkage, visible protected admin row, adoption, readiness states | A single-user household shows its owner; mismatched usernames no longer imply stray accounts; no upgrade changes user data |
| 3 | Managed owner-vault credential lifecycle, rotation, recovery, backup/restore | Encrypted data survives normal changes, resets, failures, and cross-box restore |
| 4 | Fresh-install owner provisioning and consistent handover/copy | Enabled services are usable with documented credentials; reruns create no duplicates |
| 5 | Administrative Nextcloud migration CLI and recovery runbook | Interrupted transfers resume safely; source retirement requires complete verification |

Changes 3 and 4 form one product release: do not enable automatic owner-vault provisioning before
the lifecycle guarantees are implemented. Migration tooling is independently useful and does
not block the first-install fix. Keep each change surgical; no adjacent cleanup or new dependencies
unless a concrete implementation need is demonstrated.

Primary touchpoints: `src/household.py`, owner/Vault/setup routes in `src/app.py`,
`src/vault_account.py`, `src/member_escrow.py`, `src/selftest.py`, `src/activation.py`,
`src/integrations.py`, dashboard/credential-sheet assets, and the deployment, master-password
rotation, backup, and restore scripts. Locate the post-health deployment hook before wiring the
new provisioning step; the low-level database bootstrap script alone is not that hook.

## 6. Verification matrix

Run service integration/E2E checks on disposable stacks, including the pinned production
versions. Add no unit tests. Keep credentials and decrypted test values out of logs.

| Scenario | Required evidence |
|---|---|
| Fresh install, vault enabled | One owner row; one usable vault; matching issued password; closed public signups; correct handover |
| Retry after invite, registration, or verification interruption | Same vault UUID and key; no duplicate users or secret loss |
| Vault disabled, unreachable, or recovery setup incomplete | Explicit skipped/incomplete state; successful services remain usable |
| Legacy HA owner name and external vault email | Correct roles and explicit linkage; unchanged IDs, passwords, and data |
| Single-user household with only admin | One visible admin row with Owner badge and household total of one; no empty-household message, duplicate user, rename control, or member-only actions |
| Owner service unavailable or vault unlinked | Owner row stays visible; affected service status is unknown or unlinked; account identities remain unchanged |
| Empty vault with signups closed | Owner setup action remains available |
| Concurrent clients across all manager workers | One shared admin login on cache miss; expiry/revocation recovery; bounded 429 behavior |
| CLI absent, bad PATH, or failing command | Accurate health/endpoint result; no false successful logout |
| Password rotation and recovery reset | Fixture secrets decrypt afterward; failure/crash checkpoints resume; no stale-credential success claim |
| Independent vault password change | Managed status becomes unverifiable; no destructive reset or false recovery promise |
| Restore onto a different box/master password | Owner UUID/linkage and escrow preserved; items and attachments decrypt; no replacement account |
| Populated Nextcloud migration and destination collision | Exact content, shares, versions, trash restoration, Passwords values, and DAV data verified |
| Interrupted migration or unknown app data | Source retained; journal resumes safely or identifies unsupported work; recovery procedure succeeds |
| Owner deletion/reset bypass and phone QR request | Server enforces owner protections and existing QR restriction |

For the current production box, keep the retained cleanup backup until the user has resolved
whether to restore the deleted personal Vaultwarden account. Implementing this plan must not
silently replace those five backed-up vault items with a new empty account. Any production
adoption or restore is a separate, explicit operation.
