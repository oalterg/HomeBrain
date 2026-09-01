# Disaster recovery

Getting your data back when the box is gone — stolen, burned, drowned, or the
disk simply died. This is the procedure the off-site copy exists for.

If the **box still runs** and only the backup drive failed, you do not need this
page: open the dashboard, go to **Backup**, pick an archive marked ☁️ and press
Restore. That path is fully dashboard-driven.

---

## What you need before you start

1. **The off-site details** — type (SFTP / WebDAV / S3), host or URL, username,
   password, and remote folder. Same values you entered when you set the
   off-site copy up.
2. **The master password or the recovery phrase.** New archives (HBK1) open
   with either secret. Type whichever you have.
   - If you enter the **master password**, this box comes back on that
     password — same as before.
   - If you enter the **recovery phrase**, this box gets a **new** master
     password (shown once on the handover screen). The phrase itself stays
     the same.
   - Archives made before backup-unlock still need the master password that
     encrypted them. The recovery phrase cannot open those older files.
3. **Somewhere to put it.** The archive is downloaded before it is unpacked, so
   the new box needs free space of roughly the archive's size. A full-system
   archive can be tens of gigabytes.

---

## Procedure

The setup wizard has a **Restore system** checkbox. That is the path for a
replacement box, and it takes the backup from either place:

- **The backup drive.** Plug the old box's drive into the new one and press
  *Find my backup drive*. The wizard mounts it and lists what is on it.
- **The off-site copy.** Enter the same details the old box was using, and it
  lists the remote instead.

Then: pick the archive → the master password **or recovery phrase** → Restore.
Dual-wrapped archives open with either. If you used the master password, the
box comes back on that password. If you used the recovery phrase, the handover
screen shows a **new** master password; the phrase is unchanged.

Walked 2026-08-12 on `homebraintest.local` (RPi4, no backup drive):

- **Wizard restore** — canary survived nuclear reset → Restore system from
  off-site SFTP. The box comes back on the **old** master password.
- **Two-pass** — nuclear reset → wizard as a **new** box → dashboard
  restore from off-site with the old passphrase. Canary restored;
  dashboard login is the **new** master password. All six containers
  healthy.

The fetch staged on the internal disk (`ensure_staging_dir`). Not proven:
a WAN transfer of a multi-tens-of-GB archive.

The two-pass path below still works when the wizard is already past and the
box is up. The new box keeps **its own** master password; you type the old
one only as the archive passphrase.

### Alternative: install first, then restore from the dashboard

#### 1. Install HomeBrain on the new hardware, normally

Provision and complete the setup wizard exactly as you would for a new box.
Let it generate a new master password and write it down.

This feels wrong — you are setting up a box you are about to overwrite — but
the restore needs a running stack to restore *into*. The throwaway Nextcloud
and Home Assistant it creates are replaced wholesale in step 4.

The new box keeps its **own** master password afterwards. It does not inherit
the old one, and you do not need them to match.

#### 2. Give it somewhere to stage the download

Dashboard → **Backup** → **Storage**.

- If you have attached a backup drive, select it as usual.
- If this box has no drive, tick **"No drive — keep backups on the internal
  disk"**. The download needs a writable staging area either way.

#### 3. Point it at your off-site copy

Dashboard → **Backup** → **Off-site Copy**. Enter the details from the old box
and save, then press the connection test. You do not need to enable the
scheduled copy yet.

Once saved, archives held only at the off-site remote appear in the restore
list marked with ☁️.

#### 4. Restore

Dashboard → **Backup** → pick the ☁️ archive → **Restore**.

- Confirm the warning. It wipes the throwaway data from step 1.
- When prompted for a passphrase, enter **the old box's master password or
  its recovery phrase**.
- The archive downloads first. On a slow connection this takes a long time and
  the dashboard will sit on the restore log — that is normal. If there is not
  enough free space the restore refuses up front rather than filling the disk.

#### 5. Afterwards

- Log in with the **new** box's master password from step 1.
- Nextcloud, Home Assistant, Vaultwarden and the AI agent's workspace come back
  as they were. Issued household passwords come back too: restore re-wraps
  `member_escrow.json` under this box's recovery key, and no wrap file is left
  behind. Vaults someone changed themselves in the Bitwarden app stay theirs.
- Connected accounts (email, extra Home Assistant or Nextcloud logins) survive:
  the archive carries the key those tokens are encrypted with, which is why the
  restore works across two boxes with different master passwords.
- Set up the off-site copy properly now (enable the schedule), and generate a
  fresh recovery phrase under **Settings → Recovery Phrase**.

---

## If the restore refuses

| Message | Meaning |
| --- | --- |
| `Decryption failed — wrong passphrase or corrupt archive` | The secret is not the master password or recovery phrase that can open this archive. Legacy (pre-unlock) archives still need the master password from backup time. |
| `Not enough space to fetch …: needs N MB, M MB free` | The staging area is too small. Attach a drive, or free space, and retry. Nothing was downloaded. |
| `Failed to mount backup drive` | The box expects a drive that is not attached. Either attach it or tick the no-drive option in step 2. |
| `Could not fetch … from the off-site remote` | Credentials, host or folder are wrong, or the remote is unreachable. Use the connection test on the Off-site Copy form. |

---

## Check that your files are actually off-site

Pre-update *system snapshots* stay on the backup drive. They restore settings,
Home Assistant, and the vault — not your Nextcloud files — so they are not
copied off-site. On the Backup page, the ☁️ entries are what you would be
restoring from after losing the box; they should say "Full System". Leftover
system snapshots from older versions are removed on the next off-site copy
once a full archive is present locally.

The usual cause of a remote that only held snapshots was a too-old rclone:
versions before 1.64 cannot split a large upload into chunks, so a
multi-gigabyte archive is sent as a single request and the receiving server
rejects it with *413 Request Entity Too Large*, while the small snapshots went
through. HomeBrain now installs a current rclone before every off-site copy.
If your remote is missing full archives from before that fix, they upload on
the next copy — allow plenty of time, since a full archive can take many hours.

An upload interrupted by a restart is picked up again within the hour, and
after a reboot, by the off-site resume timer. Archives already at the remote are
skipped, so only the one that was in flight repeats.

## Known limitation

A full-system archive can be tens of gigabytes. The fetch refuses up front
if the staging disk is too small. If the wizard stages on the internal disk
it now records `BACKUP_INTERNAL=true` so the next backup does not look for
a drive that is not there.

Verified 2026-08-12: wizard restore, no backup drive, SFTP off-site (same
host), canary restored. Verified 2026-07-24: fetch from a live WebDAV
remote onto a box with a different master password, including the
cross-instance key import, up to the point of unpacking into the stack.
