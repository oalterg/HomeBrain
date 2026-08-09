# Storage on a second disk — what actually needs building

**Status:** Done — shipped in PR #169 (2026-08-09), verified on hardware. All three
steps, plus a catalog slim to two models. Kept for the reasoning, not as a to-do list;
the one open trade-off is the 27B's context window, under "What shipped".
**Date:** 2026-08-09
**Scope:** Moving user data off the system disk, and reclaiming the system disk.

**Read `AGENTS.md` first.** Anything here that touches `move_nc_data.sh`, `restore.sh` or
the drive UI is a hardware-E2E merge gate. Test box: `homebraintest.local`
(192.168.178.51). Take the lock.

---

## The investigation

The question was "let the owner move Nextcloud, and Home Assistant too if it makes
sense." Measured on production `.58` before answering (914 GB root LVM, 494 GB used,
57%):

| What | Size | Can the owner move it today? |
|------|------|------------------------------|
| `/home/homebrain/models` | **307 GB** | No |
| `/home/homebrain/nextcloud-data` | **91 GB** | Yes — `move_nc_data.sh`, shipped in #152 |
| `/var/lib/docker` (**every** volume: HA config, both DBs, Vault, Caddy) | **6.2 GB** | No |
| `/mnt/backup` (external, 938 GB) | 168 GB | Yes — already a drive |

Three conclusions, in order of how much they change the plan.

### 1. Home Assistant is not worth moving. Neither is anything else in Docker.

Every named volume on the box adds up to 6.2 GB, and HA's config is a fraction of that.
A "move HA to another disk" feature would be a bind-mount rewrite, a stop/start dance and
a restore-path change, to reclaim a rounding error. HA's only unbounded consumer is the
recorder database, and the fix for that is `purge_keep_days`, not a second disk.

**Don't build it.** This is the answer to the "if sensible" half of the question.

### 2. The largest consumer on the box is the model cache, and it is invisible.

`switch_model` is explicit about it (`utilities.sh:2492`): *"Previously-downloaded models
are kept on disk for faster switching."* Nothing ever deletes one. The picker offers
**9 models between 9.5 GB and 38 GB**, and `/api/ai/models` (`app.py:2633`) returns the
*catalog* — it never looks at the disk. So an owner who tries three models silently
spends ~80 GB and has no screen that says so and no button that reclaims it.

Caveat, stated honestly: `.58`'s 307 GB is a *dev* box carrying fourteen GGUFs from the
benchmark sweeps, not a customer's. But the mechanism is the shipped product's, and a
curious owner walks the same path more slowly.

**Moving the models to the second drive is the wrong fix.** A 30 GB model on USB 3.0 is
a ~5-minute service start against ~30 seconds on NVMe, and models are re-downloadable
artifacts, not user data. Deleting the ones you are not running reclaims 281 GB on `.58`
for a fraction of the code a move would take.

### 3. The dashboard never shows the disk that fills up.

Storage shows a **Backup drive usage** meter and a one-line "your files live on…". There
is no meter for `/`. `healthcheck.py:702` does watch it (`disk_root`, warn at 85%,
critical at 95%), so the owner's first signal that the system disk is filling is a health
card at 85% — with no breakdown of what took the space and no action attached.

### What the existing Nextcloud move gets right, and its one trap

`move_nc_data.sh` is good work and this plan does not relitigate it: mount **at** the data
dir so a missing drive trips Nextcloud's own `.ncdata` check, two-pass rsync so the outage
is minutes rather than hours, verify-before-delete, and a full revert path. Keep all of it.

It is, however, **one-way and once-only** — `move_nc_data.sh:41` refuses when the data is
already on `/mnt/nextcloud-data`. An owner whose 2 TB files drive fills up cannot move to a
4 TB one, and cannot move back to the internal disk to decommission a drive, without SSH.
For a product whose premise is "no SSH required", the second move is not a nice-to-have.

### Two data-loss edges found while reading

- **`restore.sh:223`** rsyncs the archive into `$NEXTCLOUD_DATA_DIR` with no `mountpoint`
  check. If the files drive is absent, a restore writes the whole library onto the root
  disk *at the mountpoint*, filling it, and the next boot hides it under the mount. Every
  neighbouring surface already knows to check this — `app.py:1676`, `healthcheck.py:166`,
  and #163 shipped exactly this guard elsewhere. Restore is the one that skipped it.
- **`move_nc_data.sh:68`** treats *any* drive labelled `NextcloudData` as its own
  half-finished copy and resumes onto it — skipping the format, then running pass 1 with
  `--delete`. A files drive carried over from a **dead box** to a fresh one is labelled
  exactly that, and the fresh box's data dir is empty. The pre-flights pass (a tiny source
  fits anywhere) and the library is deleted. Narrow, but it is the hardware-replacement
  path, which is precisely when someone plugs an old drive into a new box.

---

## The plan

Three steps, ordered cheapest-first. Steps 1 and 2 are independent of each other and of
step 3.

### Step 1 — Show the disk, and stop restore filling it

*Nobody can make a storage decision they cannot see.*

- New `GET /api/system/storage`: `shutil.disk_usage("/")`, plus the model cache total.
  Sum the file sizes in the flat `models/` directory — do **not** `du` the Nextcloud data
  directory, which is 91 GB of many small files and would block the request.
- Storage section: a **System disk** meter beside the existing backup meter, using the
  `setMeter` helper already there, with a `— 307 GB of that is downloaded AI models` line
  when the cache is worth mentioning. That sentence is what makes step 2 discoverable.
- `restore.sh`: before the first rsync, if `NEXTCLOUD_DATA_DIR` is not the default and not
  a mountpoint, `die` with "the files drive is not connected" — before any service stops.

Roughly 40 lines of Python, 15 of JS, 3 of bash.

### Step 2 — Let the owner delete models they are not running

*281 GB on `.58`, and the highest GB-per-line in the repo.*

- `GET /api/ai/models` gains `on_disk: [{filename, size_gb, active}]` from a listing of the
  models directory. Catalog and disk are different questions; answer both.
- `DELETE /api/ai/models/<filename>`: refuses the active `AI_MODEL_FILENAME`, refuses
  anything that is not a `*.gguf` resolving inside the models directory. No task runner
  needed — an unlink is instant.
- UI: under the model picker, the downloaded models with their sizes and a Delete button,
  disabled with "in use" on the active one. Confirm dialog says it can be re-downloaded.
- Leave whisper models alone (~150–570 MB); they are not the problem.

Deliberately **not** automatic pruning on switch. Keeping the previous model is a real
convenience for A/B-ing two models, and silently deleting a 30 GB download the owner
waited for is a worse surprise than the disk usage. Show it, offer the button.

### Step 3 — Make the files move repeatable, and close the foreign-drive trap

*The one-way street is the actual "user friendly transfer" gap.*

- Drop the `SRC != DEST` refusal (`move_nc_data.sh:41`). The invariant that matters is
  "the target is not the device currently holding the data", which is a different test.
- **Drive → drive:** the destination path is occupied by the source mount, so stage the
  new drive at `/mnt/nextcloud-data.new`, rsync/verify there, then swap: unmount both,
  point the `/mnt/nextcloud-data` fstab line at the new UUID, mount, recreate the
  container. Do **not** wipe the old drive at the end — when the source is a whole drive,
  leaving it as an unplugged cold copy is strictly better than `find -delete`. Report
  "`/dev/sda` still holds a full copy; you can unplug it."
- **Drive → internal:** `move_nc_data.sh --internal`, with the capacity check against the
  root filesystem, then drop the fstab line. Reuses the same copy/verify/switch path.
- **Resume receipt** (fixes the trap above): write `.homebrain-move-in-progress` on the
  destination naming the source path when the copy starts, remove it on success, and
  `--exclude` it from the verification diff. Resume only when the receipt is there and
  matches. A non-empty destination with no receipt is somebody else's library — refuse
  and point at Restore.
- UI: "Store files here" stays as-is on unused drives; the files-drive row gains
  "Move to another drive", and the storage line gains "Move back to the internal disk".

Largest of the three. Worth doing after 1 and 2 are live, because the System disk meter is
what tells an owner whether they need this at all.

---

## Non-goals

- **Moving Home Assistant, the databases, Vault, or any Docker volume.** 6.2 GB, all in.
- **Moving the model cache to the second drive.** Load-time regression on USB; step 2
  reclaims more for less.
- **A general "map any service to any disk" layer.** Two things are big enough to move,
  one of them already moves, and the other should shrink instead.
- **Automatic model pruning on switch.** See step 2.

## What shipped (2026-08-09)

Beyond the three steps, the catalog was slimmed to **two** models on request:
`Qwen3.6-35B-A3B-UD-Q5_K_XL` (the existing default, so production needed no model
switch) and `Qwen3.6-27B-IQ4_XS`. Seven entries removed, including `Qwen3.5-9B-MTP-Q8_0`.

**Open trade-off the slim creates:** the 27B ships `context_window: 32768`, and the
OpenClaw harness needs ≥81920. The 9B that was removed was the only *other* model
meeting that floor, so the 35B is now the only one that can properly back the agent —
the 27B is a small-box option that will under-serve it. Raising the 27B's context
re-crosses the DeltaNet MTP cliff and costs ~75% of its throughput, so this is a real
trade, not an oversight. Noted inline in `BENCHMARKS.md`.

**Found by the hardware run, fixed:** the delete endpoint's `10 per minute` rate limit
stopped a real 15-model cleanup at ten with a 429, and because the limiter replies in
HTML the UI reported it as "could not delete that model". Now 30/minute with an
explicit 429 message.

**Live results on production `.58`:** 494 GB used → **228 GB** (57% → 26%), 266.6 GB
reclaimed through the API, two model files left, llama-server still answering from the
kept 35B.

## Verification

Per `AGENTS.md`, on `homebraintest.local` with the lock held — all executed 2026-08-09:

| # | Case | Result |
|---|------|--------|
| 1 | Foreign `NextcloudData` drive offered as target | Refused; seeded file survived; `.env` untouched |
| 2 | internal → drive (real USB, live Nextcloud) | 79 files moved; only `nextcloud.log` differed (the running server's own log); internal dir emptied |
| 3 | drive → internal | 78 files **byte-identical** per-file; fstab entry dropped; old drive kept all 79 files and its UUID |
| 4 | Restore with the files drive absent | Refused before `Stopping services`; containers untouched |
| 5 | Delete active model / traversal / non-gguf | 409 / 400 / 400 |

Regression suite after the changes: 165 pytest, 9 bash suites (186 assertions) incl.
`test_backup_retention.sh`, ruff + compileall + shell=True gates — all green.

Original acceptance criteria, kept for the record:

1. Fill the system disk meter with a known file; confirm the percentage and the models
   line match `df` and `ls`.
2. Delete a non-active model from the UI; confirm the active one cannot be deleted and
   that AI still answers afterwards.
3. Restore an archive with the files drive physically unplugged — must refuse before
   stopping a single container, and leave the box serving.
4. Move internal → drive A → drive B → internal, checking file counts and one file's
   checksum at each hop, with `NEXTCLOUD_DATA_DIR` and fstab read back each time.
5. Plug a drive labelled `NextcloudData` carrying files into a box with an empty data
   directory and press "Store files here" — must refuse, with the drive untouched.
