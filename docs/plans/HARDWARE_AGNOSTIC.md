# Hardware Agnosticism — Design & Implementation Plan

**Status:** Phases 0–5 implemented on `feat/hardware-agnostic` (2026-07-25). Phase 6 still blocked
on hardware. Not yet verified on the production box — see §8.
**Date:** 2026-07-25
**Context:** HomeBrain assumes "x86_64 + AMD discrete GPU + 16 GB VRAM + Vulkan" in six places.
An NVIDIA target (DGX Spark: aarch64 + CUDA + 128 GB unified memory) surfaced them, but two of
the six are bugs on *any* NVIDIA box today. This plan replaces the assumptions with one
explicit platform record rather than adding a second code path.

---

## 1. Goals & Non-Goals

### Goals
- **One place decides what hardware this is.** Today `common.sh` and `app.py` disagree
  (bash: non-x86 ⇒ no GPU; python: `nvidia` is a valid compute driver). One implementation, one answer.
- **Every hardware-varying decision keys off that record**, not off `uname -m` as a proxy.
- **Additive, not migratory.** Existing `platform_models.json` entries, the AMD box's flags, and
  the shipped `.env` keep working byte-for-byte. The correct result of Phases 0–2 on the
  production AMD box is *no observable change*.
- **Testable without the hardware.** Detection must be exercisable in CI against fixture sysfs
  trees, because we do not own a Spark and probably never will.
- **Fix the two live NVIDIA bugs** this exposes (§3.1, §3.2) — they are the near-term payoff.

### Non-Goals
- No driver-registry / plugin architecture. There are three drivers (`amdgpu`, `nvidia`, `none`)
  and a `case` statement is the right shape. Do not build for a fourth we do not have.
- No autotuning of llama.cpp flags. Profiles stay hand-measured and committed, exactly as today.
- No ROCm, Metal, Intel oneAPI, or CPU-inference backend until someone has the hardware to
  benchmark. Vulkan and CUDA only.
- Not making HomeCloud (no-GPU) run the AI stack. `HAS_GPU=false` keeps meaning what it means.
- Not solving Proton Bridge on arm64 (§6). Surfacing it honestly is in scope; fixing it is not.
- Not buying a DGX Spark. Phase 6 is contingent, not committed.

---

## 2. The platform record

One function, one record, two consumers.

```
detect_platform()                  # scripts/common.sh — the only probe in the repo
  HB_ARCH        x86_64 | aarch64
  HB_GPU_DRIVER  amdgpu | nvidia | none      # basename of /sys/class/drm/renderD*/device/driver
  HB_GPU_BACKEND vulkan | cuda | none
  HB_GPU_MEMORY  discrete | unified | none   # decides whether VRAM telemetry is meaningful
  HAS_GPU        derived: [[ $HB_GPU_DRIVER != none ]]
```

`HAS_GPU` stays exported and keeps its exact current meaning, so all ~15 existing call sites
(`provision.sh:93`, `utilities.sh:1226/1534`, `deploy.sh:175/215`, `backup.sh:208/289/409`,
`update.sh:226`, `rotate_master_password.sh:158`, `healthcheck.py:459`) are untouched by this plan.
That is deliberate: it keeps the diff to the *decisions*, not the call sites.

**Platform tag** — the string used as a config key throughout: `"${HB_ARCH}-${HB_GPU_BACKEND}"`,
e.g. `x86_64-vulkan` (today's production box), `aarch64-cuda` (Spark).

**Bash is authoritative.** `detect_platform` runs on every `common.sh` source (it is a handful of
sysfs reads) and emits `/opt/homebrain/.platform.json`. `app.py` reads that file; if it is absent
it shells out to `bash -c 'source common.sh && emit_platform_json'` rather than re-implementing
the probe. This ordering matters: `common.sh` is sourced by `provision.sh` *before* dependencies
are installed, so the earliest consumer cannot depend on Python.

**Testability requirement.** `detect_platform` reads sysfs through `${HB_SYSFS_ROOT:-}`, so
`scripts/tests/test_platform_detect.sh` can point it at a fixture tree. Non-negotiable — it is the
only way any of this gets verified before hardware exists.

### Why not a driver abstraction layer

Considered and rejected. The variance is four decisions (binary, flags, telemetry, hardening),
not a coherent interface. A `gpu_driver` interface with four methods and two implementations is
more code than four `case` statements and hides which decision is which. If a fourth driver ever
lands, revisit — not before.

---

## 3. What changes, by decision

### 3.1 GPU telemetry — **fixes a live bug**

`_gpu_vram_used_mb()` (`utilities.sh:1046`) and `get_gpu_stats()` (`app.py:104`) read
`mem_info_vram_used/total`, an amdgpu-only sysfs interface.

On any NVIDIA box today: the reader returns `0`, `verify_llama_allocation` (`utilities.sh:1069`)
compares `0 < min_healthy_vram_mb: 15000`, declares the server degraded, **restarts llama-server
twice on every start**, and then serves anyway behind an alarming log line.

| driver | reader |
|---|---|
| `amdgpu` | existing sysfs path, unchanged |
| `nvidia` | `nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader,nounits` — one subprocess, no new dependency |
| `none` | `{"available": false}` |

`verify_llama_allocation` gains one guard: **when the reader reports no telemetry, skip the check**
rather than failing it. A watermark you cannot measure is not a watermark.

On `HB_GPU_MEMORY=unified` there is no discrete VRAM at all — "used / total VRAM" is the wrong
model. The dashboard card is relabelled *GPU memory* and, when unified, presented as a share of
system memory. Template + one JSON key.

### 3.2 Binary acquisition — **fixes a real inconsistency**

Two half-implementations of the same job exist today:

| function | pin | arch |
|---|---|---|
| `install_llamacpp` (`utilities.sh:718`) — update path | honours `versions.json` | **ignores arch**, downloads the x64 URL verbatim |
| `install_llama_prebuilt` (`utilities.sh:801`) — provision path | **ignores the pin**, fetches `latest` | maps `uname -m` → `x64`/`arm64` |

So a fresh provision and an update install different builds. Collapse to **one** function that
honours both. `versions.json` gains a per-tag asset map:

```json
"llama_cpp": {
  "tag": "b9381",
  "assets": {
    "x86_64-vulkan":  "https://…/llama-b9381-bin-ubuntu-vulkan-x64.tar.gz",
    "aarch64-vulkan": "https://…/llama-b9381-bin-ubuntu-vulkan-arm64.tar.gz"
  },
  "source_build": {
    "aarch64-cuda": {
      "cmake_flags": "-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121a -DGGML_CUDA_FA_ALL_QUANTS=ON -DCMAKE_BUILD_TYPE=Release"
    }
  }
}
```

Verified: `llama-b9381-bin-ubuntu-vulkan-arm64.tar.gz` exists upstream. **No Linux CUDA prebuilt
exists at any tag** — only Windows CUDA zips — so `aarch64-cuda` must build from source. That path
reuses the pattern `install_whisper_server` (`utilities.sh:1092`) already uses: clone at a pinned
ref, cmake, install. No new machinery. Unknown-tag → `die` with the platform tag named, so an
unsupported box fails loudly at provision instead of silently landing on the wrong binary.

### 3.3 Model flag profiles — the one schema change

Every entry in `platform_models.json` encodes a 16 GB cliff: `-ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU"`
pushing half the MoE experts to CPU, q4/q8 KV to fit, `context_window` capped at 81920 because 96K
evicts, `--threads 6` for a 6-core Ryzen. `extra_flags` is declared per-model but is genuinely
per-model×platform.

Additive fix — an optional `profiles` object, with today's top-level fields as the default:

```json
{
  "id": "Qwen3.6-35B-A3B-UD-Q5_K_XL",
  "context_window": 81920,
  "extra_flags": "…-ot \"blk.(2[0-9]|3[0-9])…\" --threads 6 …",
  "profiles": {
    "aarch64-cuda": {
      "context_window": 131072,
      "extra_flags": "--parallel 1 --jinja -ngl 999 -fa on --no-mmap --cache-type-k q8_0 --cache-type-v q8_0 -b 2048 -ub 2048 --threads 10 …"
    }
  }
}
```

Resolution in `setup_llama_server` (`utilities.sh:955-968`): `profiles[<tag>] // <top-level>`.
Every existing entry is untouched and resolves exactly as it does today. On a 128 GB unified box
the `-ot` offload, the KV downgrade and the context cap all simply disappear from the profile —
this is the mechanism by which the machine's advantages become usable at all.

`min_healthy_vram_mb` moves under the profile too, since it is a property of the fit, not the model.

### 3.4 Host hardening — stop applying amdgpu fixes to non-AMD hardware

`provision.sh:107-146` fires on `HAS_GPU == true` and writes `amdgpu.runpm=0 amdgpu.pg_mask=0`
into GRUB, runs `update-grub`, installs the amdgpu udev rule, masks Navi 44 VCN/JPEG via modprobe,
and runs `update-initramfs -u`. On non-AMD hardware these are no-ops at best; writing amdgpu kernel
params and regenerating the initramfs on an unfamiliar distro is not something to do blind.

Split by driver:
- `harden_gpu_amdgpu` — everything currently there, verbatim, moved.
- `harden_gpu_nvidia` — verify driver + container toolkit are present; log and continue. Nothing invasive.
- The firewall rules and `systemctl disable apache2` **move out of the GPU gate entirely**. They
  are not GPU-related; they are gated there by accident today, which means a no-GPU HomeCloud box
  never gets its firewall opened.

### 3.5 Branding

`inject_platform` (`app.py:369`) brands by `platform.machine()`, so an aarch64 box with a GPU would
call itself "HomeCloud" while running the full agent. Key it off `has_gpu()` — which is what the
distinction has always actually meant.

### 3.6 Browser

`common.sh:555` hardcodes `google-chrome-stable_current_amd64.deb`. Google ships **no** Linux arm64
Chrome. Fall back to `chromium` (arm64 exists in the Ubuntu archive) and point OpenClaw at the
resolved binary. Already non-fatal; this makes it work rather than warn.

---

## 4. Phasing

Each phase ships independently and is verifiable **on the AMD box we own**, where the correct
outcome for Phases 0–2 is *nothing changed*. That constraint is what makes this safe to do without
target hardware.

| # | Phase | Verification | Status |
|---|---|---|---|
| **0** | `detect_platform` + record emission + `HB_SYSFS_ROOT` fixtures. No consumer changes. | `test_platform_detect.sh` in CI, 11 detection cases | ✅ |
| **1** | Telemetry reader (§3.1) + `verify_llama_allocation` skip-when-unmeasurable. `app.py` and `healthcheck.py` read the record instead of re-probing. | Prod: GPU card identical, no extra llama restarts | ✅ |
| **2** | Collapse the two llama install functions into one; per-tag asset map (§3.2). | Both pinned asset URLs return HTTP 200 | ✅ |
| **3** | `profiles` resolution in `setup_llama_server` (§3.3). | All 9 models resolve byte-identically on `x86_64-vulkan`; asserted in CI | ✅ |
| **4** | Hardening split, firewall de-gating, branding, chromium (§3.4–3.6). | Prod: grub, udev rule, modprobe conf unchanged after re-provision | ✅ |
| **5** | Source-build path for `aarch64-cuda` (§3.2), Proton Bridge notice (§6). | Shellcheck + review only — **the build path has never been executed** | ✅ |
| **6** | *Contingent on hardware.* Spark bring-up: benchmark the `aarch64-cuda` profile, add a BENCHMARKS.md section, update TESTING.md pre-flight. | Real hardware, per TESTING.md | ⛔ blocked |

Landed as ~580 insertions / ~220 deletions across 14 files plus one new test, of which a large
share is code *moving* (the amdgpu hardening block) rather than being added.

Two things came out differently from the plan:

- **`healthcheck.py` was a third probe**, not two. It globbed `/dev/dri/renderD*`, the loosest test
  of the three — an RPi's VideoCore would have passed it. Now reads the record.
- **Hybrid boxes needed a preference order.** Once the driver identity started selecting a binary
  and a hardening path, "first render node wins" became wrong: an iGPU usually takes `renderD128`
  and would beat the discrete card next to it. Detection now prefers `nvidia > amdgpu > xe > i915`.
  Covered by two fixture cases.

---

## 5. Open questions — need a real box, do not guess

- Does DGX OS report `ID=ubuntu` in `/etc/os-release`? `common.sh:572` builds the Docker apt repo
  from it. Docker also ships preinstalled on Spark, so the repo setup may need to become a no-op.
- Does `nvidia-drm` expose `/dev/dri/renderD128`? Both `detect_platform` and the unit's
  `ExecStartPre` wait (`config/llama-server.service`) depend on it. If not, detection needs a
  second signal and the unit needs a driver-aware gate.
- Does `nvidia-smi` report meaningful `memory.used/total` on a unified-memory part, or does it need
  a different query? Determines whether §3.1's nvidia reader is correct or merely plausible.
- Is a Vulkan ICD present on DGX OS at all? Decides whether `aarch64-vulkan` is a usable fallback
  while the source build is being sorted, or whether CUDA is the only path.

---

## 6. Known unfixable-here: Proton Bridge

`shenxn/protonmail-bridge:3.19.0-1` is **amd64-only**. Every other image in `docker-compose.yml`
(mariadb, redis, nextcloud, home-assistant, caddy, vaultwarden, newt, cloudflared) publishes arm64.

It is profile-gated, so an arm64 box comes up healthy and only Connections → Email is missing.
In scope for this plan: **say so**. The Email connection card checks the platform record and shows
"not supported on this architecture" instead of leaving the user with a container that will not start.
Out of scope: sourcing or building a replacement image.

---

## 7. Why bother

The Spark-shaped motivation is real but secondary. On the hardware we already own this plan:

- kills a double-restart bug that would hit the first NVIDIA box we ever touch (§3.1),
- resolves the provision-installs-`latest` / update-installs-the-pin split (§3.2),
- ends the bash-vs-python disagreement about what a GPU is (§2),
- stops gating the firewall behind GPU presence (§3.4).

The hardware agnosticism falls out of fixing those. That is the correct order of operations, and
the reason this is worth doing before any hardware decision is made.

For the record on the hardware question itself: DGX Spark is bandwidth-bound at 273 GB/s, *below*
the RX 9060 XT's 320 GB/s, so it loses on dense models. HomeBrain's default is a ~3B-active MoE,
which is the shape that wins there — llama.cpp's own Spark bench for Qwen3-Coder-30B-A3B Q8_0
reports pp2048 ≈ 2987 t/s (vs our 696) and tg ≈ 30 t/s at 32K depth (vs our 29.1 at 80K, with half
the experts on CPU). Prompt processing — the metric that dominates an agent harness re-ingesting a
60K context — is the ~4-5× win. Deep-context generation is roughly a wash, at a higher quant. The
blocker is that it is a ~$4,000 machine for a home appliance, which is a product decision, not an
engineering one.

---

## 8. Hardware verification — `.58`, 2026-07-25 ✅

Verified on the production AMD box (Ubuntu 25.10, RX 9060 XT, stable `v2026.07.21`).

| Check | Result |
|---|---|
| Platform record on real hardware | `x86_64-vulkan / amdgpu / discrete / has_gpu=true` — matches the old `HAS_GPU` exactly |
| Fixture suite on a box **with** a GPU | 17/17 (see below — this caught a test bug) |
| `harden_gpu` on the amdgpu arm | grub, udev rule, modprobe conf all md5-identical afterwards |
| llama-server restarts caused | **0** (`NRestarts=0`, unchanged `ActiveEnterTimestamp`) |
| Dashboard GPU card | `available:true, memory_label:"VRAM"`, temp/util/VRAM all populated |
| Branding | `HomeBrain` (keyed off `has_gpu()`, not arch) |
| `healthcheck.py` off the record | GPU-gated checks ran; `services` + `openclaw` both OK |
| Unified installer | resolved `x86_64-vulkan`, installed, recorded the tag, short-circuited on re-run |
| Browser path | resolves to `/usr/bin/google-chrome-stable`, no longer hardcoded in the seed |

**The `.installed_versions.json` prediction was right** — the file did not exist on `.58`, so the
box's binary had indeed been installed by the old `install_llama_prebuilt`, which never recorded a
tag. The unified installer did exactly one install and has short-circuited since.

**A test bug the hardware caught.** `probe()` claimed to shadow the machine's real `lspci`, but the
replacement `PATH` still contained `/usr/bin` — which is where `lspci` lives. Two cases asserting
"no compute GPU present" passed on a macOS dev box (no `lspci` exists there) and failed on the
Linux target, where the fallback correctly found the real Radeon. The fixture PATH is now fully
self-contained: it symlinks in only the tools `detect_platform` shells out to, so "no lspci" means
no lspci. This is the argument for running the fixture suite on the target, not just in CI.

### Follow-up worth doing (found during verification, not fixed here)

`verify_llama_allocation`'s watermark is a proxy that can miss the thing it exists to catch. `.58`
was found serving **degraded** — 24 t/s against a starved compute buffer (`606 MiB, does not match
expectation of 1344 MiB`) — and the check passed anyway, because total VRAM used (15330) cleared
`min_healthy_vram_mb` (15000). The compute buffer is not visible in that number. The reliable
signal is the journal line; grepping the unit's log after start would catch what the watermark
cannot. See `docs/BENCHMARKS.md` (2026-07-25).

## 9. aarch64 verification — RPi4, 2026-07-25 ✅

Raspberry Pi 4 Model B, aarch64, Debian 13 (trixie), kernel 6.12.47. This board is the risk case,
not a proxy for it: it **does** expose `/sys/class/drm/renderD128` bound to `v3d`, and `lspci`
**is** installed. Both rejection layers had to work for the answer to be right.

```
saw node: /sys/class/drm/renderD128/device/driver -> driver=v3d
  -> rejected (not in the amdgpu|nvidia|i915|xe compute allow-list)
lspci fallback: only a PCIe bridge and a USB controller, no VGA/3D/Display line -> no match
```
```
{"arch":"aarch64","gpu_driver":"none","gpu_backend":"none","gpu_memory":"none",
 "platform_tag":"aarch64-none","has_gpu":false}
```

Fixture suite: **17/17 on real aarch64**, no arch-specific failures — including
`aarch64 + v3d (RPi VideoCore) -> none` passing on an actual VideoCore.

**Bug found and fixed during this run.** `common.sh` opened with an unconditional
`export INSTALL_DIR="/opt/homebrain"`, so the documented probe-in-isolation recipe
(`export INSTALL_DIR=/tmp/... ; source common.sh`) silently did not isolate — the variable was
overwritten at source time, and safety came only from `emit_platform_json -` printing to stdout.
Calling `emit_platform_json` without the `-` would have written the live record on a box being
probed. Now `${INSTALL_DIR:-/opt/homebrain}`.

Caveat: that Pi runs an Apr-26 `openclaw-integration` checkout with no `version.json`, so this
verified the probe **against** aarch64 hardware, not the end-to-end consequence of `has_gpu=false`
flowing through `provision.sh` / `app.py` there. Provisioning it is a separate exercise.

### Still outstanding

- **RPi4 end-to-end**: provision the board and confirm the no-GPU path (HomeCloud branding, AI
  units never installed, healthcheck skipping GPU checks) actually follows from the record.
- The **`aarch64-cuda` source-build path** remains unexecuted — no hardware.

