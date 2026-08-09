# Phone Photos — hardening follow-ups

Written 2026-08-09, after a review of the photo path shipped in #156 (Phone Photos) and #157
(household members). Findings were verified against the live box, not read off the source.

Item 1 has a design and is ready to build. Items 2 and 3 are recorded with their evidence so the
next session does not have to rediscover them; neither is scheduled.

---

## 1. A phone must never hold an admin credential *(S — the security item)*

### What is wrong

`photos_pair` (`src/app.py:4270`) pairs the phone as `NEXTCLOUD_ADMIN_USER`. On the production box
that account is `admin`: member of the `admin` group, `quota: none`, and the account the Nextcloud
`passwords` app (2026.7.20) is encrypted under — `SSEv1UserKey`, i.e. server-side, so the key is
unwrapped server-side on API access.

A Nextcloud app password authenticates as that user with that user's full rights. It is not scoped
to the Files API. So the QR code the card invites you to hold up to a phone camera is a permanent,
non-expiring admin credential for Nextcloud *and* everything the `passwords` app holds, usable from
anywhere over the tunnel.

Household members are already modelled correctly — their own account, no admin group, no dashboard,
no Vault. The owner is the one identity that is not.

### Why the fix is nearly free

`admin` has no data to protect *as a file account*:

| account | used | share of data |
|---|---|---|
| `admin` | 36,654,063 B (~35 MB — the three Nextcloud sample PDFs) | 0.01% |
| `OliAidana` | 91,453,453,867 B (~85 GiB) | 18.25% |

The owner is **already** keeping their real photo library in a non-admin household account. The
admin pairing path is not carrying anyone's data; it is a special case that only carries risk.

### The design

Delete the special case. There is no "the owner's phone" — there are people, and a phone belongs to
a person. `admin` becomes what it always should have been: a service account that runs `occ`, holds
the Vault, and is never handed to a phone.

1. **One guard makes the invariant unbypassable.** `pairing_payload` refuses when the user is
   `NEXTCLOUD_ADMIN_USER`. It is the single funnel every pairing route already goes through, so the
   rule cannot be forgotten by a future route.
2. **Delete `POST /api/photos/pair`.** It is `repair_household_member` with the target hardcoded to
   admin. Two routes collapse to one.
3. **Extract the two server settings.** `app:enable photos` and the 2048 preview caps are not
   per-pairing facts; they are box settings that happen to be asserted at pair time. Move them to
   `ensure_photo_settings()` and call it from the member pair path. Still idempotent, still
   self-healing, no longer bundled with credential minting.
4. **Rewire the card.** The Phone Photos card keeps its copy — it explains the product, and that is
   worth keeping — but its button becomes "Add a phone", which is the Household form. Each person in
   the Household list gets "Show code", which is `repair_household_member`. Already built.

Net: one route deleted, one guard added, one helper extracted, one card rewired. No new dependency,
no new concept — it removes one.

### What it buys

A lost or stolen phone exposes one person's camera roll and nothing else: no admin, no Vault, no
other member's files. "Who holds a credential on this box" becomes answerable by reading the
Household list, which is a list of people, instead of being invisible.

### Non-goals

- **Not** naming, expiring, or adding a revoke UI for app passwords. Real, but separate (see below).
- **Not** moving the Vault or re-keying the `passwords` app.
- **Not** sharing a photo folder back to `admin`. Nobody needs it — each person signs into their own
  account. Worth stating because it is the obvious wrong turn: `occ` has no share-create command
  (only `share:list` and `files:transfer-ownership`), so building this would mean an OCS HTTP call
  from inside the manager, and the reason to avoid it is that the requirement is imaginary.

### Migration

For a box where a phone was paired as `admin`, the existing app passwords stay live after the
update. The update **must not** revoke them itself — it cannot tell a phone's token from the
`passwords` app's own session, and revoking the wrong one logs the owner out of their password
manager. Surface it instead: tell the owner to revoke stale devices in Nextcloud → Settings →
Security. For file data, `occ files:transfer-ownership admin <member>` covers boxes where `admin`
accumulated more than sample PDFs.

### Done when

- `pairing_payload` refuses `NEXTCLOUD_ADMIN_USER`, with a test asserting it.
- No route in `app.py` can mint a credential for the admin account.
- The Phone Photos card pairs a named person, and pairing a new phone needs no admin password.
- On the production box, `occ user:auth-tokens:list admin` shows no device tokens after re-pairing.

---

## 2. No quotas anywhere, on the disk that also runs the house *(recorded, not scheduled)*

`grep -rn quota` over `src/` and `scripts/` returns nothing. On the live box `files default_quota` is
unset, `admin` is `none`, and `OliAidana` is `default` — i.e. unlimited.

`NEXTCLOUD_DATA_DIR` is `/home/homebrain/nextcloud-data`, which is exactly `NC_DATA_DEFAULT`
(`healthcheck.py:44`), so photos land on the root LV — 914 GB, 57% used, 382 GB free — the same
filesystem as Docker, Home Assistant, the manager, and backup staging.

Any member's camera roll can therefore fill `/`. That does not merely stop photo upload; it takes
down Home Assistant, the dashboard, and the backup job together. `check_files_drive` correctly
returns `None` in this configuration (`healthcheck.py:164`) because `disk_root` covers the space —
but `disk_root` warns at 85% and crits at 95%, which is detection, not prevention, and 95% of 914 GB
still leaves only ~45 GB of runway.

Two independent mitigations, either of which helps:

- Set a finite `files default_quota` before more members are added. Cheap now, awkward once people
  are over the line, and it wants a field in the Household card rather than an `occ` invocation.
- Adopt the dedicated files drive from #152, which this box is not using. It converts "the house
  goes down" into "photo uploads stop" — the failure you want.

---

## 3. HEIC and video have no thumbnails *(recorded, not scheduled)*

`enabledPreviewProviders` is unset, so Nextcloud's built-in default list applies. Read from
`PreviewManager.php` on the box, that list is `PNG, JPEG, GIF, BMP, XBitmap, Krita, WebP` plus
`MarkDown, TXT, OpenDocument`. There is no `OC\Preview\HEIC` and no `OC\Preview\Movie`.

Counted in the live data directory:

| type | files | previews today |
|---|---|---|
| `.jpg` / `.jpeg` / `.png` | 11,023 | yes |
| `.heic` | 583 | **no** |
| `.mp4` / `.mov` | 1,199 | **no** |

So ~1,782 files — about 14% of the media on the box — render as generic file icons in the Photos
grid. HEIC is the default capture format on every current iPhone, so this is the newest content and
the fastest-growing share.

The two halves have very different costs:

- **HEIC is one config line.** `imagick` in the container already reports `HEIC`, `HEIF`, and `AVIF`
  delegates. Adding `OC\Preview\HEIC` alongside the existing 2048 preview cap in
  `ensure_photo_settings()` costs nothing and needs no new dependency. Note that setting
  `enabledPreviewProviders` at all *replaces* the default list, so the full set must be written, not
  just the addition.
- **Video is not free.** `ffmpeg` is missing from the Nextcloud container, so `OC\Preview\Movie`
  needs it added to the image first. That is a new dependency in the container, which is exactly the
  kind of change `PRODUCT_REVIEW_2026-07.md` says to weigh rather than assume.

This one sits badly against the card's own copy — "replace Google Photos and iCloud". The grid is the
product, and for an iPhone household it is partly blank.

---

## Also found, not scheduled

- **No liveness signal for uploads.** Nothing on the dashboard reports when a photo last arrived.
  Every failure mode is silent: paired against a LAN address so it never syncs off-network, Auto
  Upload switched off by an app update, iOS suspending background upload, the credential revoked.
  This is the class of bug #160 fixed for off-site backups — silence looking like health — left
  unfixed on the ingest side. The cheap version is the newest mtime under each user's
  `files/InstantUpload`, shown on the card.
- **App passwords accumulate unnamed.** Each press mints another and the old one stays live
  (`app.py:4237`). Tokens land without a device name, so "revoke the phone I lost" is guesswork, and
  there is no revoke UI outside Nextcloud's own settings. The card says pressing again "issues a new
  one" but not that the previous one keeps working.
