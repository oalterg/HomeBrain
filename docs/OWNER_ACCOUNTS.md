# Owner accounts and maintenance

Household shows the configured Nextcloud owner first, labeled **admin — Owner** on a
standard installation. The owner counts toward the household total. Service details
show the actual Nextcloud login, linked vault email and Home Assistant owner login.
HomeBrain does not rename or merge existing accounts during an upgrade.

The owner has no member deletion, password-reset or phone-pairing controls. Phone QR
pairing still requires a separate non-admin member because an owner's token grants
administrative access.

## Vault setup and adoption

Fresh deployments invite and register `admin@homebrain.local` (or the configured
owner's synthetic email), with public registration closed. The vault initially uses
the HomeBrain master password. The Vaultwarden administration token is a separate
server credential; it does not open a personal vault.

On an existing installation, open **Household → Manage owner vault**. Explicitly
choose an existing vault or create one. Linking preserves its UUID, email, items and
password. Only select password management if you want HomeBrain to include the vault
in master-password changes and recovery; a working vault password and enabled backup
recovery are required. PBKDF2 accounts are supported. Accounts using other KDFs or
requiring two-factor login can be linked with independent password management.

If the vault password changes directly in a Bitwarden client, HomeBrain cannot reset
it using its administration token. Verify access in Household and relink with the
current credential. A matching email alone never authorizes takeover of an account.

Do not create an empty vault to replace a retained backup. In particular, the five
items retained from the earlier production cleanup need a separate adoption/restore
decision before creating a replacement production owner vault.

## Password changes and recovery

Managed vault changes retain the existing encryption key. Before the remote change,
HomeBrain seals both credentials using the existing recovery escrow and records a
checkpoint under `/var/lib/homebrain/owner_vault.json`. It verifies the new login
and key before promoting the new credential. The journal contains identity and a
key fingerprint, not plaintext credentials.

A failed cross-service rotation is reported as incomplete. Completed service changes
remain in place. Review the setup log and retry the same intended password; a pending
rotation permits that retry even if the dashboard already uses the new password.
If a vault operation is pending, finish it before choosing a different password.
Do not delete its journal or escrow to bypass an error. Normal rotation and recovery
use the same vault engine.

Backup captures the binding, pending journal and sealed credentials while holding the
same account-operation lock as rotation, restore and household mutations. Restore
loads the real vault database and rewrapped escrow before restoring the binding and
verifying access with the destination password. Old backups without owner metadata
remain unlinked. Runtime admin sessions are not included in backups.

## Nextcloud account migration

This is an administrative CLI, separate from owner setup. It never runs on upgrade.
Use the installed virtualenv as root and retain the job directory until independently
satisfied with the result. Password files must be private (mode 0600). The job directory
contains a database backup, files, configuration and sensitive verification metadata;
keep it outside shared storage and routine retention.

```sh
sudo /opt/homebrain/venv/bin/python /opt/homebrain/scripts/migrate_nextcloud_account.py inspect SOURCE DESTINATION
sudo /opt/homebrain/venv/bin/python /opt/homebrain/scripts/migrate_nextcloud_account.py run SOURCE DESTINATION \
  --job-dir /root/homebrain-migration-JOB \
  --source-password-file /root/source-password \
  --destination-password-file /root/destination-password
```

The default retains destination files and uses Nextcloud's normal transfer folder.
Add `--destination-root` to place source files directly in the destination root.
If that root has files, replacement requires the additional explicit flag
`--replace-destination-files`. Replacement preserves the previous files in the recovery
point and a sibling `files.before-homebrain-migration` directory outside the visible
Files root. Remove that sibling only after checking the recovery point. Destination
Passwords, versions and trash conflicts block migration rather than being overwritten.

Add `--retire-source` on the original run only if the source should be deleted after
verification. Otherwise it stays disabled. The destination is enabled for API
verification and use. Authentication tokens and MFA secrets are not transplanted;
clients must sign in as the destination. The configured HomeBrain owner cannot be
migrated out by this command.

The adapter handles Nextcloud 33 local storage. External, group and server-encrypted
storage, authored Talk conversations and unrecognized app-owned records block the
operation. Inspection is deliberately conservative: resolve unsupported data separately.
Calendars and address books get collision-safe names. File shares, versions, trash,
calendar/contact payloads and decrypted Passwords values are verified. Every transferred
live file is also downloaded through the destination's WebDAV API and hashed; large
migrations can therefore take substantial time.

The command uses maintenance mode for the recovery snapshot and transfer. During
transfer, a temporary config file permits **only the migration's CLI processes** to
load app commands; web requests and cron remain in maintenance. The file is removed
when writes resume. A process crash can leave maintenance enabled and accounts disabled.

### Resume and recovery

Repeat the original arguments with `resume` instead of `run`. Keep the original job
directory, root/folder mode and password files. Completed phases are not blindly replayed.
A failed preflight requires resolving the issue and starting a new job directory.
A hash, share, password or unsupported-data error prevents source retirement.

Read `journal.json` for the last phase. Command diagnostics go to a private
`/tmp/homebrain-migration-error-*` file; stdout avoids credentials and public-link tokens.
If a process died, resume before manually enabling affected accounts. If abandoning an
operation, inspect and remove the temporary `config/homebrain-migration.config.php`,
turn maintenance off with `occ maintenance:mode --off`, and deliberately choose which
accounts to enable. Retain all recovery material.

The recovery point contains the original source/destination homes, `nextcloud-config`
and `database.sqlite` or `database.sql`. First rehearse database/file restoration on an
isolated installation of the same versions. Never restore the whole database over a
running instance after unrelated users have made new writes. If native transfer stopped
inside a phase and live state cannot be reconciled, the command stops for manual recovery
rather than guessing or deleting the source.

## Reproducing service checks

Use a disposable Docker host. Copy this checkout's `src` and `scripts` into
`/tmp/homebrain-owner-e2e`, owned by root with mode 0700. The scripts refuse another
install path. They use independent compose volumes and loopback ports 18080, 18082,
18123, and 18083 for the restore instance. Never point them at production volumes.

Provide the Passwords app **code only** at a private path, then run:

```sh
export INSTALL_DIR=/tmp/homebrain-owner-e2e
export HOMEBRAIN_HOME=$INSTALL_DIR/home
export HOMEBRAIN_STATE_DIR=$INSTALL_DIR/state
export PASSWORDS_APP_DIR=/path/to/passwords-app-code
python scripts/e2e/setup_owner_stack.py
python scripts/e2e/owner_accounts.py
python scripts/e2e/owner_restore.py
python scripts/e2e/nextcloud_migration.py
python scripts/e2e/nextcloud_migration.py --folder
```

Run owner checks before creating migration fixture users. Password login rate limits
remain enabled; retries wait for the real cooldown. The setup script seeds a fixture-only
OpenClaw config and nonce to suppress unrelated manager startup reconciliation. The
browser fixture is `scripts/e2e/owner_dashboard.py` on loopback port 18000.

Cleanup is explicit: remove `homebrain-owner-e2e-restored`, run `docker compose
--project-directory /tmp/homebrain-owner-e2e down -v`, then remove the disposable directory.
Do not reuse leftover service volumes with a newly generated `.env`.
