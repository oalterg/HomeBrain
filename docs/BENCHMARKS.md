# HomeBrain AI Inference Benchmarks

## Hardware

| Component | Spec |
|-----------|------|
| GPU       | AMD Radeon RX 9060 XT (16 GB VRAM, 16,304 MiB usable) |
| CPU       | AMD Ryzen 5 5600 (6 cores / 12 threads) |
| RAM       | 32 GB DDR4 |
| Backend   | Vulkan (RADV / GFX1200) |
| OS        | Ubuntu 24.04 (x86_64) |
| llama.cpp | **b9186** in production (history: b8951 → b8996 → b9186; the Results-table rows below were measured on b8951/b8996). b9297 and b9381 evaluated 2026-05-28 — see version + MTP sections. |
| RADV env  | `RADV_PERFTEST=rm_kq=1` (set via systemd drop-in) |
| Mesa      | 25.2.8 (Ubuntu 25.10) |
| Kernel    | Linux 6.17 |

## Methodology

- TG = text generation throughput (tokens/sec), 256-token completion, ~26-token prompt
- PP = prompt processing throughput (tokens/sec), measured at ~2.4k-token prompt
- Target throughput for production use: 20 t/s (text generation)
- Captured via `/v1/chat/completions` `timings.predicted_per_second` / `prompt_per_second`
- Bench harness: `bench.sh <label> <model> <ctx> -- <flags...>` — starts llama-server, polls `/health`, runs PP probe + TG probe, captures VRAM via `/sys/class/drm/card1/device/mem_info_vram_used`, kills server, appends JSONL.

## Results

| Model                   | Quant      | Size  | Ctx  | KV   | -ot range | -b/-ub      | TG (t/s) | PP@2k (t/s) | VRAM     | Date       |
|-------------------------|------------|-------|------|------|-----------|-------------|----------|-------------|----------|------------|
| Qwen3.6-35B-A3B         | UD-Q4_K_M  | 21 GB | 131K | q8_0 | 23-39     | 4096 / 4096 | **34.13**| **866**     | ~15.3 GB | 2026-05-01 |
| Qwen3.6-35B-A3B         | UD-Q5_K_M  | 25 GB | 131K | q8_0 | 20-39     | 4096 / 4096 | 29.09    | 760         | 15.0 GB  | 2026-05-01 |
| **Qwen3.6-35B-A3B**     | UD-Q5_K_XL | 26 GB | 131K | q8_0 | 20-39     | 4096 / 4096 | 29.20    | 751         | ~15.0 GB | 2026-05-01 |
| Qwen3.6-35B-A3B         | UD-Q6_K    | 29 GB | 131K | q8_0 | 18-39     | 4096 / 4096 | 27.18    | 689         | ~15.6 GB | 2026-05-01 |
| Qwen3.6-35B-A3B         | UD-Q6_K_XL | 32 GB | 131K | q8_0 | 16-39     | 4096 / 4096 | 25.38    | 643         | ~15.8 GB | 2026-05-01 |
| Qwen3.6-35B-A3B         | UD-Q8_K_XL | 38 GB | 131K | q8_0 | 14-39     | 4096 / 2048 | 17.96    | 140         | ~15.9 GB | 2026-05-01 |
| Qwen3.6-27B (DeltaNet)  | IQ4_XS     | 14 GB | 48K  | q4_0 | (none)    | 4096 / 2048 | **17.17**| 435         | 16.0 GB  | 2026-05-01 |

UD-Q5_K_XL remains the production default — best quality/throughput balance. Q4_K_M is the highest-throughput option (~+18% TG, +6% PP over Q5_K_XL) at the cost of perceptible quality loss; useful for latency-sensitive workloads. Q5_K_M and Q5_K_XL are statistically tied — XL is preferred for slightly better quantization quality.

## Tuning Log

`-ub 4096` retune sweep (2026-05-01). Same `-ot` ranges as the prior tuning pass; only `-ub` changed (or kept where higher value regresses).

| Model     | Run                                | TG    | PP@2k  | Verdict |
|-----------|------------------------------------|-------|--------|---------|
| Q4_K_M    | -ot 23-39, -b 4096 -ub 4096        | 34.41 | 802    | **Best of all 35B quants — kept** |
| Q4_K_M    | -ot 22-39, -b 4096 -ub 4096        | 33.48 | 718    | Worse on both axes |
| Q4_K_M    | -ot 24-39, -b 4096 -ub 4096        | 33.92 | 743    | More CPU offload, TG dips |
| Q5_K_M    | -ot 20-39, -b 4096 -ub 4096        | 29.09 | 760    | **Kept — matches Q5_K_XL** |
| Q5_K_M    | -ot 21-39, -b 4096 -ub 4096        | 22.99 | 174    | VRAM 15.9 GB, throttled |
| Q5_K_M    | -ot 22-39, -b 4096 -ub 4096        | 21.25 | 172    | More CPU offload, no recovery |
| Q5_K_XL   | -ot 20-39, -b 4096 -ub 4096        | 29.23 | 751    | **+31% PP vs -ub 2048 (574 → 751)** |
| Q6_K      | -ot 18-39, -b 4096 -ub 4096        | 27.18 | 689    | **+34% PP vs -ub 2048 (513 → 689)** |
| Q6_K_XL   | -ot 16-39, -b 4096 -ub 4096        | 25.38 | 643    | **+33% PP vs -ub 2048 (485 → 643)** |
| Q8_K_XL   | -ot 14-39, -b 4096 -ub 4096        | 16.10 | 132    | VRAM 99.8% (16,271 / 16,304 MiB), regression on both axes |
| Q8_K_XL   | -ot 14-39, -b 4096 -ub 2048        | 17.96 | 140    | **Kept — only 35B quant where -ub 2048 still wins** |

27B IQ4_XS phase-1 sweep (2026-05-01, b8951). DeltaNet-safe variants only (no `-ot`).

| Run                          | TG    | PP    | Verdict |
|------------------------------|-------|-------|---------|
| -t 6 -b 2048 -ub 1024 (prod) | 14.89 | 442   | Phase-1 winner |
| -t 6 -b 2048 -ub 2048        | 14.96 | 433   | TG flat, PP -2% |
| -t 6 -b 4096 -ub 2048        | 14.96 | 432   | No gain |
| -t 5 -b 2048 -ub 1024        | 14.88 | 443   | Tied with prod |
| -t 5 -b 2048 -ub 2048        | 14.96 | 433   | No gain |
| -t 6 -b 4096 -ub 4096        | 14.95 | 416   | PP -6%, regression |

Phase 1 conclusion seemed to be that DeltaNet's TG path is GPU-fused and indifferent to `-b`/`-ub`/`-t`. **That conclusion was wrong** — TG was capped by VRAM headroom, not by the fused-kernel logic. See phase 2.

27B IQ4_XS phase-2 sweep (2026-05-01, b8996 + `RADV_PERFTEST=rm_kq=1`). Same DeltaNet-safe core; ctx and KV-type swept to free VRAM headroom.

| Run                                       | ctx | KV   | -b/-ub      | TG    | PP    | VRAM     | Verdict |
|-------------------------------------------|----:|------|-------------|-------|-------|----------|---------|
| baseline (phase-1 winner, on b8996)       |  64K| q4_0 | 2048 / 1024 | 14.75 | 434   | 16.24 GB | Reproduces phase-1 baseline within noise |
| ctx 16K + same flags                      |  16K| q4_0 | 2048 / 1024 | 17.09 | 444   | 15.53 GB | **+16% TG** by reclaiming 700 MB VRAM |
| ctx 16K + b 4096 / ub 2048                |  16K| q4_0 | 4096 / 2048 | 17.17 | 433   | 15.32 GB | TG holds, PP flat |
| ctx 16K + b 4096 / ub 4096                |  16K| q4_0 | 4096 / 4096 | 17.15 | 427   | 16.18 GB | Larger ub re-introduces VRAM pressure |
| ctx 32K + b 4096 / ub 2048                |  32K| q4_0 | 4096 / 2048 | 17.17 | 435   | 15.64 GB | Same TG as 16K, twice the context |
| ctx 16K + KV q8_0 + b 2048 / ub 1024      |  16K| q8_0 | 2048 / 1024 | 17.19 | 445   | 15.79 GB | Better KV numerics, perf unchanged |
| ctx 16K + KV q8_0 + b 4096 / ub 2048      |  16K| q8_0 | 4096 / 2048 | 17.19 | 434   | 15.57 GB | tied for best |
| ctx 32K + KV q8_0 + b 4096 / ub 2048      |  32K| q8_0 | 4096 / 2048 | 17.22 | 436 | 16.15 GB | Tied for best TG; only 32K context |

27B IQ4_XS phase-3 ctx-extension sweep (2026-05-01, b8996 + `rm_kq=1`). Same `-b 4096 -ub 2048` and KV q4_0 — finds the largest ctx that holds peak TG.

| ctx | TG    | PP  | VRAM    | Verdict |
|----:|------:|----:|--------:|---------|
| 32K | 17.18 | 434 | 15.64 GB | safe |
| 40K | 17.15 | 434 | 15.78 GB | safe |
| **48K** | **17.17** | **435** | **16.00 GB** | **Kept — peak TG, max ctx** |
| 56K | 16.75 | 434 | 16.22 GB | -2.4% TG, VRAM 99.5% |
| 64K | 14.93 | 434 | 16.25 GB | -13% TG, the original throttle cliff |

Net win on the 27B: **+15.4% TG (14.89 → 17.17), PP flat, ctx 64K → 48K (-26%) but still ample for the home-automation agent.** KV stays at q4_0 (50% more headroom-per-token vs q8_0); upgrading to q8_0 buys slightly better attention numerics but costs the extra 16K of context. The picked trade prefers context length on this card. Same VRAM-pressure pattern as the 35B-A3B quants — DeltaNet's TG was VRAM-bound, not kernel-bound, just hidden behind the fused-kernel regression mask.

Tuning principles confirmed on this hardware:
- **`-ub 4096` is the right default for non-XL MoE quants on this GPU.** It improves PP by 30–34% on long prompts vs `-b 2048 -ub 1024`, with TG flat or slightly improved across Q4_K_M / Q5_K_M / Q5_K_XL / Q6_K / Q6_K_XL.
- **Q8_K_XL is the exception**: at `-ub 4096` VRAM hits 99.8% and both axes regress. Kept at `-ub 2048`.
- **VRAM headroom matters more than maximizing on-GPU layers.** At >97% VRAM use, both TG and PP collapse from allocation thrashing — moving 1–2 more blocks to CPU recovers everything. Q5_K_M sweep shows this cleanly: -ot 20-39 (15.0 GB used) → 29.09 TG / 760 PP, but -ot 21-39 (15.9 GB) → 22.99 TG / 174 PP.
- **`-ot blk.(N..)` and `--n-cpu-moe N` are NOT equivalent.** `-ot` puts the *last* N layers' MoE on CPU; `--n-cpu-moe` puts the *first* N. Performance differs by 30%+ — the last layers offload better on this hardware.
- **`--threads-batch 12` is catastrophic on Ryzen 5 5600**: TG drops 29.67 → 22.63, PP 574 → 160. SMT siblings contend with main inference threads. Stick with `--threads 6` (no `--threads-batch`).
- **`--prio 2`, `--mlock`, `--no-mmap`** were all flat or slightly negative on this rig. Only `-ub 4096` (and the hardware-bound `-ot` choice) move the needle.
- Empirical -ot offload boundaries for 131K ctx + q8_0 KV cache on 16 GB VRAM:
  - Q4_K_M (21 GB): blk.23-39 (17 of 40 on CPU)
  - Q5_K_M (25 GB) / Q5_K_XL (26 GB): blk.20-39 (20 of 40)
  - Q6_K (29 GB): blk.18-39 (22 of 40)
  - Q6_K_XL (32 GB): blk.16-39 (24 of 40)
  - Q8_K_XL (38 GB): blk.14-39 (26 of 40) — drops below 20 t/s, not recommended for production
- 27B IQ4_XS uses no `-ot` (DeltaNet hybrid — partial expert offload breaks the fused kernel).

## Upgrade sweep — b8951 → b8996 (2026-05-01)

Same hardware, same model files, same flags. Bench harness identical run-to-run. Tested whether the b8951 → b8996 upgrade and the documented `RADV_PERFTEST=rm_kq=1` / PCIe-ASPM-performance levers were worth applying.

| Run                                      | TG    | PP    | Δ TG   | Δ PP   |
|------------------------------------------|-------|-------|--------|--------|
| Q5_K_XL b8951 (baseline)                 | 29.23 | 748.9 | —      | —      |
| Q5_K_XL b8996 vanilla                    | 29.14 | 690.0 |  −0.3% |  −7.9% |
| Q5_K_XL b8996 + `rm_kq=1`                | 29.29 | 748.8 |  +0.2% |   0.0% |
| Q5_K_XL b8996 + `rm_kq=1` + ASPM perf    | 29.20 | 751.4 |  −0.1% |  +0.3% |
| Q4_K_M  b8951 (baseline)                 | 34.41 | 802   | —      | —      |
| Q4_K_M  b8996 + `rm_kq=1`                | 34.13 | 866   |  −0.8% | **+8.0%** |

Findings:
- **b8996 alone regresses PP −7.9%** on Q5_K_XL. `RADV_PERFTEST=rm_kq=1` recovers it (the new build apparently relies on RADV's faster KQ matmul path being enabled).
- With `rm_kq=1` set, the upgrade is **net-positive on Q4_K_M (+8% PP)** and neutral on Q5_K_XL. TG is flat across all configurations (within ±1%).
- **PCIe ASPM=performance** is noise on this workload. Not worth a system-level change. (Documented in the literature as +10–14% on R9700 / gfx1201 with Mesa 25.3-devel; this rig runs Mesa 25.2.8 / gfx1200.)
- Production switched to b8996 with `RADV_PERFTEST=rm_kq=1` set via systemd drop-in (`/etc/systemd/system/llama-server.service.d/10-radv-perftest.conf`). Old binary preserved at `/home/homebrain/ai-runtime/llama-server.b8951.bak` for rollback.

ROCm backend was investigated but not benchmarked: llama.cpp issue [#21376](https://github.com/ggml-org/llama.cpp/issues/21376) (open) reproduces a hard OOM on RX 9060 XT when KV cache approaches the VRAM ceiling — exactly our 35B + q8_0 KV + `-ub 4096` regime. Comparative benchmarks on RDNA4 (RX 9070 XT, gfx1201) currently show Vulkan ~30% ahead of ROCm for dense Qwen3. Re-evaluate after #21376 fixes and ROCm 7.1+ gfx1200 tuning lands.

## Sampler tuning — `presence_penalty` and `reasoning_budget` for A3B

All 35B-A3B entries in `platform_models.json` carry `--presence-penalty 1.5 --reasoning-budget 8192` in addition to the model card's `temp=1.0 / top_p=0.95 / top_k=20 / min_p=0`. Rationale:

- The Qwen3.6-35B-A3B model card explicitly recommends `presence_penalty=1.5` for thinking mode — the dense Qwen3.6-27B card recommends `0`. The A3B MoE variants are documented to fall into infinite `<think>` loops on tool-call failures (HF discussions #19, #20 on `Qwen/Qwen3.6-35B-A3B`; QwenLM/Qwen3.6 #145; ollama #14421/#14493). Without `presence_penalty=1.5` the model can spend its entire context budget retrying the same failed tool call.
- `--reasoning-budget 8192` is a hard cap on think tokens; on overflow llama-server appends `</think>` and forces the assistant turn. Prevents a single bad turn from burning the 131K context window. Per-request override via `thinking_budget_tokens` in the JSON body.
- The 27B IQ4_XS entry (DeltaNet hybrid, dense Qwen3.6-27B) is left at `presence_penalty=0` per its own model card.
- Do not lower `temperature` to fight loops — Qwen explicitly warns greedy / low-temp causes more repetition on these models, not less.

## Notes

### AMD Vulkan / GFX1200 constraints
- `--cache-type-v q8_0` without `-fa` fails context creation on AMD Vulkan — flash attention is mandatory with quantized KV cache.
- DeltaNet hybrids (Qwen3.6-27B) need `-ngl 99` and no expert offload — `nkvo` and partial offload break the fused kernel.
- `amdgpu.runpm=0` (set via `config/99-amdgpu-runpm.rules`) must remain to prevent VRAM eviction during inference.

### Qwen3.6-35B-A3B MoE specifics
- Architecture: 40 layers, 256 experts (8 active per token), n_embd=2048, n_head=16, n_head_kv=2.
- KV cache at 131K ctx with q8_0: ~1.4 GB on GPU.
- Q5 quants stay under VRAM limit at -ot 20-39; Q6_K needs 18-39 to leave ~700 MB headroom for compute buffers; Q8_K_XL needs 14-39 (26 of 40 layers on CPU) and is CPU-bound.

## Multi-Token Prediction (MTP) sweep — b9186 (2026-05-17)

llama.cpp [PR #22673](https://github.com/ggml-org/llama.cpp/pull/22673) (merged 2026-05-16) adds a `draft-mtp` speculative-decoding path that uses a small auxiliary head shipped inside MTP-flavored GGUFs (`unsloth/Qwen3.6-{27B,35B-A3B}-MTP-GGUF`). The unsloth release claims 1.4–2× generation speedup on dense models and 1.15–1.25× on MoE.

Hardware: RX 9060 XT + Ryzen 5 5600, Vulkan + RADV_PERFTEST=rm_kq=1, llama.cpp b9186, docker stack running (prod-realistic), 3 deterministic 512-token runs per cell.

### 27B (dense DeltaNet hybrid) — large win

| `--spec-draft-n-max` | TG t/s | Acceptance | Δ vs baseline |
|---|---:|---:|---:|
| baseline (no MTP) | 17.24 | — | — |
| **n=2** | **25.55** | 61.9% | **+48.2%** |
| n=3 | 25.14 | 53.4% | +45.9% |
| n=4 | 23.23 | 45.3% | +34.7% |
| n=6 | 10.56 | 34.2% | −38.7% |

Three interleaved baselines (17.21 / 17.25 / 17.26) and two n=3 reruns (both 25.14) confirm stability. Adopted **n_max=2** in `platform_models.json`.

> **⚠️ Correction (2026-05-28):** The two conclusions below ("27B win" and "35B MTP net negative") were both measured *at one fixed context per model* and are now known to be context-dependent artifacts. The 27B "+48%" figure was taken below the MTP context cliff; the 35B "net negative" sweep was run entirely at production ctx=131072, above it. See [MTP re-investigation — the context-size cliff](#mtp-re-investigation--the-context-size-cliff-b9186-2026-05-28) for the corrected picture. The raw numbers in the two tables below are still valid *at the context they were measured at*; the generalized verdicts are not.

### 35B-A3B (MoE) — MTP is net negative in every config

Cross-product of quant × CPU-expert-offload depth × KV cache, with and without MTP n=2. Each row is the mean of 3 runs; baselines were re-measured between MTP runs to control for thermal drift.

| Quant | KV | `-ot` pattern | Baseline | MTP n=2 | MTP Δ |
|---|---|---|---:|---:|---:|
| **Q4_K_M** | q8 | blk.23-39 (prod, 17 layers offl.) | **33.68** | 32.53 | −3.4% |
| Q4_K_M | q8 | blk.20-39 (20 layers) | 31.86 | 31.61 | −0.8% |
| Q4_K_M | q4 | blk.23-39 | 29.26 | 31.65 | +8.2% |
| Q4_K_M | q4 | blk.20-39 | 31.81 | 30.43 | −4.3% |
| Q4_K_M | q4 | blk.18-39 (22 layers) | 30.70 | 29.46 | −4.0% |
| Q4_K_M | q4 | blk.15-39 (25 layers) | 28.98 | 29.27 | +1.0% |
| **Q5_K_XL** | q8 | blk.20-39 (prod) | 28.40 | 27.45 | −3.3% |
| Q5_K_XL | q8 | blk.20-39, n=3 | 28.41 | 25.69 | −9.6% |
| Q5_K_XL | q4 | blk.20-39 | 28.69 | 27.96 | −2.5% |
| Q5_K_XL | q4 | blk.25-39 | 22.05 | 20.83 | −5.5% |
| Q5_K_XL | q4 | blk.28-39 | 16.12 | 17.23 | +6.9%¹ |
| Q5_K_XL | q4 | blk.30-39 | 16.55 | 18.17 | +9.8%¹ |
| Q5_K_M | q4 | blk.25-39 | 24.44 | 21.31 | −12.8% |
| Q5_K_M | q4 | blk.20-39 | 29.03 | 26.83 | −7.6% |

¹ MTP relative win exists only where baseline is already crippled by VRAM-ceiling thrash (`-ot` keeps more weight on GPU than fits). Absolute TG is still worse than the unconstrained config.

**Findings**
- The current production config (Q4_K_M + q8 KV + `-ot blk.23-39`) at **33.68 t/s** is the absolute speed champion. MTP hurts it by 3.4%.
- The dense-vs-MoE gap matches unsloth's prediction *direction* (large on dense, small on MoE) but our MoE result is below their 1.15× floor. Hypothesis: heavy `-ot ffn_.*exps=CPU` defeats MTP's parallel verification — the CPU expert step is serial regardless of how many tokens you draft, so the speculative path adds draft cost without amortizing the verify cost. Their numbers likely assume all weights on GPU.
- `--spec-draft-p-split` and `--spec-draft-n-min` are no-ops for `draft-mtp` — verified by identical accept counts across all values. The flags only apply to `draft-simple` / `draft-eagle3`.
- Q5_K_M is dominated by Q4_K_M at the same offload + KV across the board; not worth shipping.

**Decisions**
- 27B IQ4_XS: switch to MTP-GGUF + `--spec-type draft-mtp --spec-draft-n-max 2`. Stored as Phase-4 in `platform_models.json`.
- 35B-A3B (all quants): **do not enable MTP.** Keep current configs.
- llama.cpp tag bumped to b9186 in `versions.json` (first tagged release with the merged PR).

## MTP re-investigation — the context-size cliff (b9186, 2026-05-28)

Follow-up to the question: *can MTP be enhanced by sweeping `--spec-draft-n-max`, or is VRAM-headroom pressure holding it below the unsloth-advertised speedup?* And separately: *does 35B-A3B Q5_K_XL MTP really never help?*

Method: `scripts/bench-upgrade.sh` — for each cell, restart llama-server clean (waiting for VRAM to drain <500 MiB), warm up, then 3× deterministic 512-token `/completion` probes with a raw chat-template prompt (bypasses thinking mode), reading `timings.predicted_per_second`. Server flags held at production-realistic `-fa on --cache-type-k q4_0 --cache-type-v q4_0 -t 6 -b 4096 -ub 2048`. Same hardware, b9186, docker stack up.

### Answer 1 — it is NOT VRAM headroom. It is a context-size cliff.

27B-MTP (n=2, predictable prompt), context swept while everything else held constant:

| ctx | TG (t/s) | VRAM used | Headroom |
|----:|---------:|----------:|---------:|
| 8K  | **29.89** | 16,032 | 272 MiB |
| 16K | **29.96** | 15,884 | 420 MiB |
| 32K | **29.96** | 16,222 | **82 MiB** |
| 49K (prod) | **17.45** | 15,722 | **582 MiB** |

This kills the VRAM-pressure hypothesis outright: the **32K cell ran at the tightest headroom of the whole sweep (82 MiB) yet held full speed (29.96)**, while the 49K cell had *7× more* headroom (582 MiB) and collapsed to 17.45. More free VRAM, worse throughput — the opposite of a headroom effect. The break is a hard cliff somewhere between 32K and 49K of allocated context.

### Answer 2 — n_max sweeping can't rescue a cell that's already over the cliff.

27B-MTP n_max sweep, all at the production ctx=49152 (predictable prompt):

| `--spec-draft-n-max` | TG (t/s) |
|---|---:|
| 1 | 15.89 |
| 2 | 17.43 |
| 3 | 16.81 |
| 4 | 16.77 |

Everything sits in the 16–17 band — no n_max value recovers the lost speed, because the limiter is the context the verify pass runs over, not the draft depth. (Below the cliff the n_max curve from the original sweep still applies: n=2 is the sweet spot, n≥6 collapses.)

### Why the production 27B sees almost no MTP benefit

Production runs the 27B at **ctx=49152 — above the cliff.** Re-measuring base vs MTP n=2 at that exact context, across prompt types:

| Prompt | Base | MTP n=2 | Δ |
|--------|-----:|--------:|---:|
| predictable | 17.15 | 17.50 | +2.0% |
| code        | 17.14 | 18.71 | +9.2% |
| creative    | 17.14 | 16.27 | −5.1% |

Net ≈ break-even. The original sweep's "+48% (25.55 t/s)" was real but measured *below* the cliff; at the shipped 49K context the MTP head is essentially dead weight (and slightly negative on unpredictable output). **The +75% that MTP can deliver only materializes at ctx≤32K** (17.15 base → ~29.96 with MTP).

### Answer 3 — 35B-A3B Q5_K_XL MTP DOES help, below the cliff

The earlier "MTP net negative in every config" sweep was run entirely at production ctx=131072. Re-run at ctx=32768 (Q5_K_XL, `-ot blk.20-39`, q4 KV, predictable prompt):

| Config | TG (t/s) | Δ vs base |
|--------|---------:|----------:|
| base       | 28.65 | — |
| MTP n=2    | 34.76 | **+21.3%** |
| MTP n=3    | 36.13 | **+26.1%** |

Same offload, same KV, same prompt as the negative rows in the prior section — the *only* change is context (131K → 32K), and MTP flips from −2.5% to +26%. So the prior "MoE architecture defeats MTP / CPU-expert step is serial" hypothesis was wrong: **it was the context, not the MoE.** Below the cliff, MTP helps the 35B too, and unlike the 27B it keeps climbing to n=3.

### Unifying conclusion

> **Update (later same day):** the conclusion in this paragraph holds **on b9186**, but the 35B half of it was a llama.cpp bug, not a hardware limit. On **b9381** the MoE cliff is gone and 35B MTP scales to 131K (+31% in production). The DeltaNet 27B cliff is real and remains on b9381. See [b9381 fixes MoE MTP](#b9381-fixes-moe-mtp--the-flagship-gets-31-2026-05-28). Read the paragraph below as the b9186 picture.

On b9186, MTP speedup is **gated by allocated context length, not by VRAM headroom and not by dense-vs-MoE architecture.** Below ~32K context, MTP delivers large gains on both models (27B +75%, 35B +26%); at the contexts we actually ship (27B 49K, 35B 131K) the speculative verify pass — whose cost scales with sequence length (DeltaNet SSM state for the 27B, attention for the 35B) — eats the draft savings and MTP regresses to break-even or worse. Tuning `--spec-draft-n-max` only moves the needle *below* the cliff; over it, no draft-depth setting helps. (The 35B "attention" half of this turned out to be the fixable bug; the 27B DeltaNet half is genuine.)

### Actionable implications (not yet applied)

- **27B IQ4_XS:** ships `--spec-type draft-mtp --spec-draft-n-max 2` at ctx=49152 — i.e. it pays for the MTP GGUF and gets ≈+2% for it (49152 is over the cliff). Dropping `context_window` 49152 → 32768 (keeping n_max=2 + q4 KV) unlocks **+75% TG (17.1 → ~30 t/s)** at the cost of 16K context. For the home-automation agent 32K is likely ample; **worth a decision.** *(Correction, 2026-08-09: "32K is likely ample" is wrong — the OpenClaw harness needs ≥81920 and this was settled 2026-06-17. It mattered little while the 27B was one option among nine; now that the catalog ships only the 35B and this model, the 27B is the box's only small option and it cannot properly back the agent. Raising its context re-crosses the DeltaNet cliff and costs the +75%, so this is a genuine trade, not an oversight to fix silently.)* Do **not** also raise n_max or switch to q8 KV — either change re-crosses the speculative ceiling and collapses TG back to ~17. If 49K context must stay, MTP buys nothing there and the plain IQ4_XS GGUF would do. See [27B cliff localization & parameter tuning](#27b-cliff-localization--parameter-tuning-b9186-2026-05-28).
- **35B-A3B:** *(superseded — this was the b9186 finding)* On b9186 MTP only paid off at ctx≤32K. On **b9381 the cliff is fixed** and MTP gives +31% at the full 131K production context — see [b9381 fixes MoE MTP](#b9381-fixes-moe-mtp--the-flagship-gets-31-2026-05-28). The flagship default should move to b9381 + MTP-GGUF + n=2 + `-ub 2048` (pending sign-off).

### 27B cliff localization & parameter tuning (b9186, 2026-05-28)

Follow-up: locate the cliff precisely and find the params that maximize TG at the maximum context that still accelerates. Script: `scripts/bench-27b-cliff.sh` (Pass 1 = ctx sweep, Pass 2 = edge + tuning). All cells MTP, predictable prompt, 3 runs.

**Pass 1 — where does it break?** (MTP n=2, q4 KV, ub2048)

| ctx | TG (t/s) | headroom | |
|----:|---------:|---------:|--|
| 32768 | **29.99** | 118 MiB | accelerated |
| 33792 | **29.96** | 85 MiB | accelerated |
| 34816 | **29.92** | 58 MiB | accelerated |
| 36864 | 17.52 | 93 MiB | **collapsed → baseline** |
| 40960 | 17.47 | 737 MiB | collapsed |
| 45056 | 17.49 | 661 MiB | collapsed |
| 49152 | 17.47 | 584 MiB | collapsed |

The cliff sits **between 34816 and 36864 tokens**. Past it, MTP TG snaps to the plain-baseline ~17.5 t/s (27B base measured 17.1 at ctx=40960 — i.e. MTP gives essentially nothing over the cliff). Headroom is irrelevant: the collapsed 40K–49K cells had 6–8× more free VRAM than the still-accelerated 34816 cell.

**Pass 2 — tuning at/below the cliff**

| Config (ctx=32768 unless noted) | TG (t/s) | Note |
|---|---:|---|
| n_max=1 | 25.67 | underspeculates |
| **n_max=2** | **29.99** | **optimal** |
| n_max=3 | 16.91 | **collapsed** |
| n_max=4 | 16.78 | collapsed |
| n=2, KV q8_0 | 17.21 | **collapsed** — q8 KV kills acceleration |
| n=3, KV q8_0 | 16.87 | collapsed |
| n=3, code prompt | 19.80 | (n=3 already collapsed; realistic-prompt sanity) |

**Mechanism.** The acceleration is governed by a **fixed speculative budget** that is consumed jointly by (context length) + (draft depth `n_max`) + (KV-cache bytes-per-token). Cross any of the three thresholds and llama.cpp silently stops speculating and falls back to plain autoregressive decode (~17.5 t/s):
- raising ctx past ~35K (at n=2, q4) → collapse;
- raising n_max to ≥3 (at ctx=32768, q4) → collapse;
- switching KV q4→q8 (which doubles cache bytes, at ctx=32768, n=2) → collapse.

This unifies every "cliff" observation: it is not VRAM headroom, not the MoE-vs-dense split, and not prompt content — it is a single allocation ceiling. **The fast operating point is the corner just under that ceiling: ctx ≤ ~34816, n_max=2, KV q4_0** → ~30 t/s (+75% over the 17.1 baseline). For a round, comfortably-margined production value use **ctx=32768**.

### Version check — b9186 vs b9297 vs b9381 (latest)

Two newer releases were A/B'd against the shipped b9186 under server-realistic, production-identical flags (`scripts/bench-version.sh`, same harness as the production rows: TG = 256-tok gen on a short prompt, PP = ~1.5k-tok prompt). b9186 baselines reproduce the documented production figures (35B 29.15 TG, 27B 17.13 TG), confirming the harness.

| Build | 35B Q5_K_XL (ctx 131K, prod) | 27B IQ4_XS (ctx 49K, prod) |
|---|---|---|
| | TG / PP | TG / PP |
| b9186 (shipped) | 29.15 / 599 | 17.13 / 430 |
| **b9381 (latest)** | 29.41 / 584 | 17.20 / 428 |
| Δ | **+0.9% / −2.5%** | **+0.4% / −0.4%** |

- b9297 (built to `~/bench-upgrade/b9297/`) showed the same flat picture — the movement seen in an earlier llama-bench-only pass did not translate to server throughput.
- **For the *vanilla* (non-MTP) path, b9381 is performance-neutral**: every delta is inside run-to-run noise (TG stdev ≤ 0.1).

**But this is not the whole story.** b9381 also *fixes MoE MTP at high context* — a change that is worth far more than any vanilla delta. See [b9381 fixes MoE MTP — the flagship gets +31%](#b9381-fixes-moe-mtp--the-flagship-gets-31-2026-05-28) below. The version decision turns on whether we want that win, not on vanilla throughput. The latest binary is staged at `~/bench-upgrade/b9381/`.

## b9381 fixes MoE MTP — the flagship gets +31% (2026-05-28)

Re-running the MTP experiment on the latest binary (b9381) instead of b9186 changes the 35B conclusion completely. **The "MTP context cliff" for the MoE model was a llama.cpp bug, fixed upstream between b9186 and b9381.** On b9381 the 35B-A3B has *no cliff through 131K*:

| ctx | base (b9381, q4/ub2048, greedy) | MTP n=2 | MTP n=3 |
|----:|--------------------------------:|--------:|--------:|
| 32768 | 29.17 | 34.43 (+18%) | 35.21 (+21%) |
| 49152 | 29.38 | 39.03 (+33%) | 35.17 (+20%) |
| **131072** | 29.37 | **38.81 (+32%)** | 34.97 (+19%) |

Compare b9186, where 35B MTP at 131K was *negative*. The DeltaNet **27B is not fixed** — on b9381 it still collapses past ~35K (MTP n=2 @ 49152 = 18 t/s, and PP even degrades to ~55 t/s). So the upstream fix is specific to the MoE/attention speculative path, not the DeltaNet SSM path.

### Deployable production config (validated, production-faithful)

The numbers above are greedy (max acceptance, q4 KV). Production runs **q8 KV, `-ub 4096`, and `temp=1.0`** sampling. Re-measured under the *exact* production flags on b9381 (3 runs, real = the prod sampler with `presence_penalty 1.5` on a natural code-gen prompt):

| Config (ctx 131072, prod samplers) | greedy TG | **real TG** | VRAM | Δ real |
|---|---:|---:|---:|---:|
| base, q8, `-ub 4096` | 28.81 | 28.71 | 15470 | — |
| MTP n=2, q8, **`-ub 4096`** | — | — | — | **OOM** (`vk::DeviceLostError`) |
| base, q8, `-ub 2048` | 28.94 | 28.81 | 15604 | — |
| **MTP n=2, q8, `-ub 2048`** | 42.93 | **37.70** | 16202 | **+30.9%** |
| base, q4, `-ub 2048` | 28.71 | 28.62 | 15984 | — |
| MTP n=2, q4, `-ub 2048` | 43.02 | 36.81 | 16210 | +28.6% |
| MTP n=3, q4, `-ub 2048` | 42.26 | 31.46 | 16021 | +9.9%¹ |

¹ n=3 wins on greedy but loses on the real sampler — at `temp=1.0` the deeper draft's acceptance drops faster than the extra-token payoff. **n=2 is optimal in production.**

**Findings**
- MTP at the *current* production config (q8 + `-ub 4096`) **OOMs** — the MTP draft head needs ~600 MiB that the `-ub 4096` compute buffer is already using. Dropping to **`-ub 2048` frees exactly enough** and keeps q8 KV (best numerics). Fits at 16202 / 16304 MiB.
- **Net win: 28.81 → 37.70 t/s real (+31%) on the flagship at full 131K context** on *structured/code/tool* output, with one model swap + two flag changes. Greedy upper bound is +48%.
- **Gain is workload-dependent.** MTP acceptance tracks output predictability: high on code/JSON/tool-calls (the agent's bread-and-butter, +20–31%), but on free-form *prose* a live test generated at **28.5 t/s ≈ base** (≈neutral). MTP never regressed below base in any test — so this is a win-or-neutral change, weighted toward the agent's typical structured traffic.
- `-ub 2048` vs `-ub 4096` costs ~24% PP on long prompts (≈751 → ≈575 t/s, per the tuning log). For a generation-bound agent the TG gain dominates; for prompt-heavy batch use weigh the PP loss.
- **VRAM (initial reading — later found unstable, see "compute-buffer eviction" below).** A fresh start measures 16205 / 16304 MiB. This was *misread* as "rock-stable": KV at 131072 is **not** fully pre-allocated, it grows with use, and at 99.4% occupancy that growth evicts the compute buffer. The ~37.7 real figure here is the **fresh-restart** number; sustained throughput under real use needed the q4-KV fix below. (whisper resident is ~56 MiB; the earlier "~500 MiB whisper" alarm was a one-off transcription spike, not resident footprint.)

### Production change (applied 2026-05-29)

Applied to capture the flagship win:
- `versions.json`: `llama_cpp.tag` **b9186 → b9381**.
- `platform_models.json`: **added** model `Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL` (MTP GGUF, `--spec-type draft-mtp --spec-draft-n-max 2`, `-ub 2048`, `-ot blk.20-39`, ctx 131072, full samplers) and made it the default. The base `Qwen3.6-35B-A3B-UD-Q5_K_XL` entry is **kept** so rollback is a single `/api/ai/model/switch` back to it (no version revert needed).
- `platform_models.json`: 27B `Qwen3.6-27B-IQ4_XS` `context_window` **49152 → 32768** to bring it under the DeltaNet MTP cliff (+75% TG when 27B is selected). b9381 does **not** fix the DeltaNet cliff — that change is the b9186 analysis and stands.

Deployed via the manager update path (pulls `main`, installs b9381, restarts) + a model switch to the MTP entry. Rollback path: `POST /api/ai/model/switch {"model_id":"Qwen3.6-35B-A3B-UD-Q5_K_XL"}`.

> **Correction (2026-05-30):** the MTP entry originally shipped at **ctx 131072**, which proved unstable (see "compute-buffer eviction" below) — the +50% decayed to break-even under real use. A first attempt dropped KV to q4_0 (still 131072); it tested stable on a clean box but **still evicts** once the GPU has accumulated session state (VRAM fragmentation / GTT pressure) — 131072 is simply not robustly stable for MTP on this 16 GB card. The shipped config is **q8 KV at ctx 65536**, verified eviction-proof across two consecutive 27K-token stress prompts. The VRAM-settle gate and `verify_llama_allocation` guard were added in the same change. Trade accepted: half the max context for a *sustained* (non-decaying) +50%.

### Compute-buffer eviction — why the 131072 win wasn't real, and the ctx-65536 fix (found 2026-05-29/30)

A follow-up A/B on the **live** server surfaced a trap: the q8/131072 config delivers +50% only for the first few minutes after a restart, then decays to ≈break-even under real use. Two faces of the same root cause (the config sits at **99.4% VRAM, ~100 MiB under the 16,304 MiB ceiling**):

**1. Startup starvation.** If llama-server allocates while VRAM is momentarily tight — whisper mid-transcription, or the previous instance still releasing ~15 GB during a `systemctl restart` — it can't get the full ~3.6 GiB Vulkan compute buffer. It does **not** fail and does **not** reduce `-ngl` (`common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort`); it **silently shrinks the buffer** (warns only). Confirmed in the degraded server's shutdown log: `Vulkan0 compute buffer size of 776.33 MiB, does not match expectation of 3656.33 MiB`.

**2. Runtime eviction (the dominant one).** Even a cleanly-started server degrades the moment the KV cache grows. KV at 131072 is **not** fully pre-allocated — it grows with context. A single deep prompt pushes total VRAM over the ceiling and the driver evicts the compute buffer to GTT (system RAM over PCIe), where it **stays until restart**. Proven by stress test (restart → 27K-token prompt → re-measure shallow):

| Server state | greedy TG | VRAM |
|---|---:|---:|
| fresh restart | **43.5** | 16208 |
| after one 27K-token prompt | 31.7 | 15518 |
| every shallow request afterwards | **30.8** (stuck) | 15518 |

**Signature is paradoxical: the degraded server uses *less* VRAM (15518) than the healthy one (16208)** — the ~690 MiB "missing" is the compute buffer, now in GTT. A concurrent request (the server runs `--parallel 1`) triggers the same eviction. This is the real reason the first deploy under-delivered: the "+50%" was a cool-fresh-restart transient.

(Distinct from the *greedy-vs-real* gap, which is MTP draft **acceptance** — compute/sampling — and is VRAM-independent.)

**Fix: reduce context (not KV type, not expert placement).** Moving more experts to CPU (`-ot blk.18-39`, `16-39`) did **not** help — at ctx 131072 the full KV/attention reservation still evicts the buffer regardless of expert placement. Dropping KV to q4_0 at 131072 looked stable on a freshly-drained box but **evicts once the session accumulates GPU state** (it was stable in an early sweep, then evicted an hour later under identical flags — the only variable was transient driver/VRAM state). The robust lever is **context length**, because llama.cpp sizes the KV reservation and attention scratch to `n_ctx` at load — smaller ctx leaves real headroom. Stress-sweep (fresh → 27K stress → sustained; later runs are on a session-fragmented GPU = worst case):

| Config | fresh | sustained (post-stress) | VRAM after stress | verdict |
|---|---:|---:|---:|---|
| q8 / ctx 131072 (original) | 43.1 | **30.8** | 15518 (evicts) | ❌ decays |
| q4 / ctx 131072 | 43.0 | **30.9** | 14902 (evicts) | ❌ decays under real conditions |
| q4 / ctx 49152 | 34.8 | **43.0** | 15339 (stable) | ✅ |
| q4 / ctx 65536 | 43.1 | **43.1** | 15492 (stable) | ✅ |
| **q8 / ctx 65536** | 43.1 | **43.1** | 15811 (stable) | ✅ **shipped** — survives 2× 27K stress, best numerics |

At the 65536 cap, q8 KV matches q4 on speed while keeping best numerics, so q8 is shipped. `context_window` is set to 65536; 64K is ample for the home/agent workload. (If a future config genuinely needs 131072, the only stable option there is the **base** model — no MTP, ~28.8 t/s — since it has no compute buffer to evict.)

**Guards also shipped (defense-in-depth for the startup-starvation case):**
- `config/llama-server.service` — `ExecStartPre` **VRAM-settle gate**: before allocating, wait (≤120 s) until non-llama VRAM use drains below 300 MiB (whisper idle ≈ 56 MiB), so a model-switch's old instance has fully released VRAM first.
- `scripts/utilities.sh` `verify_llama_allocation()` — after the health check on the model-switch/update path, if the process came up below the model's `min_healthy_vram_mb` watermark, restart (the gate holds the retry until VRAM drains) and re-check, up to 2×; never fails the caller, logs loudly.
- `config/platform_models.json` — the MTP entry declares `"min_healthy_vram_mb": 15500` (below the ~15801 stable reading, above a starved start). Models without the field skip the check.

> Note: the gate/watermark guard the *startup-starvation* face. The *runtime eviction* face is fixed structurally by the ctx-65536 headroom above (there is no resident-buffer to evict once it fits with margin) — guards can't fix runtime eviction, only the config can.

## 2026-06-17 — Headroom-relief sweep, b9672 evaluation, and the ≥80K requirement

Goal: increase 35B-A3B TG throughput; hypothesis (user) that "headroom pressure forced lower numbers." Latest upstream **b9672** staged and evaluated against production **b9381**. **New hard constraint:** the OpenClaw harness needs **≥80K context** — the prior "64K is ample" assumption is retired, and the previously-shipped MTP default (ctx 65536) is *below* spec. All cells: RX 9060 XT 16 GB (16,304 MiB), Vulkan/RADV `rm_kq=1`, `--threads 6`, greedy 256-tok TG probe + ~6.8K-tok PP probe, captured with a parameterized JSONL bench harness (server quant×ctx×KV×`-ot`×`-ub` sweep + a deep-prompt eviction stress test).

### The dominant effect is compute-buffer eviction, not raw compute

Every config with **idle headroom below ~250 MiB collapses** (TG and PP crater together) as the Vulkan compute buffer is shrunk at load or evicted to GTT under a deep prompt — independent of build or expert placement. This is the mechanism behind "headroom pressure forced lower numbers."

**whisper-server (large-v3-turbo-q5) holds 777 MiB of VRAM resident.** With it resident, even 32K cells can't fit the `-ub 4096` compute buffer → PP throttles to ~165 t/s across the board. **Freeing whisper (run it `--no-gpu`; speech-to-text is used seldom) is the headroom unlock** → PP recovers to ~700 and the configs below become possible.

### b9672 vs b9381 — NOT an upgrade (identical configs, whisper freed)

| config | b9381 TG / PP / hr | b9672 TG / PP / hr |
|---|---|---|
| q5xl 131K ot20 ub4096 q8 | **29.28 / 612 / 832** (healthy) | 23.05 / 220 / 693 (**evicts**) |
| q5xl 32K ot20 ub4096 q8 | 29.09 / 697 / 1452 | 29.48 / 655 / 1461 |
| q5xl 32K ot24 ub4096 q8 | 22.06 / 180 / 232 (evicts) | 22.54 / 187 / 235 (evicts) |
| q6xl 32K ot18 ub4096 q8 | 23.64 / 618 / 309 | 23.62 / 436 / 322 |

b9672 is **equal at sane context** and **worse at 131K** (it idles ~140 MiB heavier → tips into eviction). No throughput reason to upgrade; **b9381 stays**. (b9381 q5xl-131K reproduces the historic 29.2 table → harness validated.)

### Base-quant knee (b9381, whisper freed, 32K, q8 KV)

| quant | best `-ot` | TG | PP | headroom | old-table @131K |
|---|---|---:|---:|---:|---:|
| Q5_K_XL | ot21 | **29.67** | 686 | 891 | 29.2 |
| Q6_K_XL | ot16 | 25.07 | 510 | 1626 | 25.4 |
| Q8_K_XL | ot14 | **21.74** | 197 | 414 | 18.0 (**+21%**) |

More experts on GPU helps **only while headroom stays > ~250 MiB**; past the knee the allocator gets erratic (Q5 ot22 → TG dips to 26.9 with PP still healthy; ot23 → PP collapses to 178). Base quants are **CPU-expert-bound, not headroom-bound** at sane context — relieving headroom makes them *stable at ceiling*, except **Q8 genuinely recovers +21%** (its old 131K number was the most throttled — the user's hypothesis confirmed, specifically for the heavy quant).

### Context sweep — the ≥80K answer (base Q5, ot20, ub4096, q8, whisper freed)

| ctx | TG | PP | headroom | deep-prompt stress |
|---:|---:|---:|---:|---|
| 32768 | 29.09 | 697 | 1452 | safe |
| 65536 | 29.33 | 688 | ~1136 | safe |
| **81920** | **29.27** | 696 | 899 | **PASS** — fresh 29.35 → 29.1 after a 61K-tok deep prompt (VRAM stable 16257) |
| 98304 | 22.71 | 224 | — | **EVICTS** (peak drops 15895→14568) |
| 131072 | 29.40 | 680 | 832 | healthy on shallow probe but allocator erratic |

**80K (81920) is the largest base-Q5 context that stays eviction-safe** — and it exactly meets the harness requirement. 96K+ evicts.

### MTP-Q5 — the real throughput lever (b9381, whisper freed, n2, ub2048)

| ctx | KV | `-ot` | TG | PP | headroom |
|---:|---|---|---:|---:|---:|
| 32768 | q8 | 20 | **38.62** | 598 | 467 |
| 65536 | q8 | 18 | 36.71 | 532 | 752 |
| 81920 | q8 | 16 | 34.66 | 541 | 64 (too tight) |
| 81920 | q8 | 18 | 36.52 | 576 | 103 (too tight) |
| **81920** | **q4** | **18** | **36.67** | 568 | **501** |
| 98304 | q8 | 16 | 34.61 | 544 | 71 (too tight) |
| 131072 | q4 | 16 | 37.49 | 549 | 31 (too tight) |

n=2 > n=3 (acceptance), consistent with prior runs. At 80K, q8 KV is too tight (hr ≤ 103); **q4 KV is what buys the headroom** that keeps MTP eviction-safe at the required context.

### Deep-prompt stress test (b9381, whisper freed, 80K, after a 61K-token prompt)

| config | fresh TG | sustained TG | VRAM after fill | verdict |
|---|---:|---:|---:|---|
| **base** q5xl 80K q8 ot20 ub4096 | 29.35 | **29.1** (−0.9%) | 16257 (stable) | ✅ PASS |
| **MTP** q5xl 80K q4 ot18 ub2048 n2 | 38.42 | **38.31** (−0.3%) | **15038 (stable, hr 1266)** | ✅ PASS |

The q4 KV at 80K keeps the cache so small that VRAM stays at 15038 even with 61K of context filled — **the compute buffer never comes under pressure**, the exact opposite of the 131K MTP eviction that forced the ctx-65536 retreat in May. With whisper off the GPU, MTP is eviction-proof at 80K.

### Conclusion & recommendation

- **Do not upgrade to b9672** — it is not faster and evicts worse at high context. Stay on **b9381**.
- **Free whisper from VRAM** (`whisper-server --no-gpu`) — the 777 MiB it returns is what makes ≥80K viable.
- **Two eviction-proof ≥80K configs:**
  - **Fast (recommended): MTP-Q5 @ 80K, q4 KV, `-ot blk.18-39`, `-ub 2048`, n=2 → 38.3 t/s sustained (+32%).** Trade: q4 KV (vs q8 "best numerics") + MTP is win/neutral by workload (structured/code/tool ≫ prose).
  - **Safe-numerics: base-Q5 @ 80K, q8 KV, `-ot blk.20-39`, `-ub 4096` → 29.1 t/s sustained.** Best KV precision, no speculation.
- Both are a large gain over the **current production reality** (base @ 131072 with whisper resident, which sits at ~55 MiB headroom and runs eviction-degraded ~23 t/s), and both satisfy the ≥80K harness requirement that the shipped MTP-65536 default did not.

### Follow-up: did b9672 actually *regress*, or are there undiscovered wins? (gold-standard re-test)

The server A/B above made b9672 *look* like a regression, but that conflated compute with eviction. Re-tested with **`llama-bench`** (no server, no KV-growth, no eviction confound) — 35B Q5, `-ncmoe 20`, q8 KV, `-t 6`, identical on both builds:

| build | batch | pp2048 | tg128 |
|---|---|---:|---:|
| b9381 | 4096/4096 | 662.3 | 20.65 |
| b9381 | 16384/2048 | 659.9 | 20.83 |
| b9672 | 4096/4096 | 662.7 | **20.91** |
| b9672 | 16384/2048 | 661.6 | 20.93 |

**b9672 is NOT a compute regression — pp and tg are identical (b9672 a hair *faster* on tg).** The only real difference is **memory footprint**: at the identical 80K config, b9672 idles at **15950 MiB vs b9381's 15405 — +545 MiB heavier**. On a 16 GB card that extra reservation is exactly what tips high-context configs over the ceiling into compute-buffer eviction (it's why b9672 "lost" at 131K in the server A/B). So **stay on b9381 because it's leaner on VRAM, not because it's faster** — there is no compute headroom to gain by upgrading.

**Undiscovered improvements? Tested the circulating RDNA4 (R9700) levers — none transfer to our VRAM-constrained offloaded regime**, because that hardware fits the whole model in 32 GB VRAM while ours offloads 20 expert layers to CPU (the bottleneck is the PCIe/CPU expert path, not GPU compute):
- `-b 16384 -ub 2048` (claimed +29% PP on 35B MoE): **flat** here (pp 662 → 660, both builds).
- PCIe ASPM `performance` (claimed +10.8% decode): **flat** (TG 28.7 → 28.7 on the live server) — the link is already pinned at 32 GT/s by `amdgpu.runpm=0` and never enters ASPM low-power during active inference.
- `rm_kq=1`: already in use (confirmed optimal in the prior upgrade sweep).
- MoE coopmat tile-geometry tweak (#22598): experimental/unmerged, Strix-Halo-gated, not applicable.

**Bottom line: no regression, no free lunch on either build for the offloaded regime.** The real throughput levers remain MTP (characterised above) and headroom discipline (whisper→CPU, ctx ≤ 80K).

## 2026-07-15 — Qwen3.5-9B Q8 sweep (small/fast model) + b9990 evaluation

Goal: a small, fast Q8-quantized model as an alternative to the 35B default, at the **≥80K context the OpenClaw harness requires**, with generous headroom (production KV growth has repeatedly caused headroom pressure). Also: re-probe upstream llama.cpp (**b9990**, latest release) against pinned b9381.

**There is no Qwen3.6-9B** — the Qwen3.6 generation is 27B + 35B-A3B only. The 9B lives in the previous generation: **Qwen3.5-9B** (unsloth GGUFs, base + MTP repos; the MTP repo reuses the base filenames, staged locally with a `-MTP-` infix). Architecture is a **hybrid like the 27B**: 32 layers = 24 linear-attention (DeltaNet-style, fixed-size recurrent state) + 8 full-attention (GQA-4, head_dim 256) → KV cache is tiny (~17 KB/token at q8_0, ~4× smaller per token than the 35B), native 256K positions. Same operating constraint as the 27B: `-ngl 99` mandatory (partial offload breaks the fused linear-attention kernel). Q8 candidates: `Q8_0` (9.53 GB) and `UD-Q8_K_XL` (12.97 GB).

Method as in prior sweeps: server cells with greedy 256-tok TG probe (×2), a "real-sampler" probe (temp 1.0, top-p 0.95, top-k 20, presence 1.5, code prompt) exposing MTP acceptance via `timings.draft_n`, ~6.8K-tok PP probe, idle/post VRAM from sysfs (total 16,304 MiB); whisper on CPU; RADV `rm_kq=1`, `-t 6`, `-b 4096`.

### Phase A — b9381, quant × KV × ub × ctx (ctx 81920 unless noted)

| quant | ctx | KV | ub | TG greedy | TG real | PP | idle VRAM | headroom |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| Q8_0 | 80K | q8_0 | 2048 | 32.41 | 31.70 | 1281 | 12836 | 3468 |
| Q8_0 | 80K | q8_0 | 4096 | 32.39 | 31.66 | 1168 | 15688 | 616 |
| Q8_0 | 80K | q4_0 | 2048 | 32.41 | 31.70 | 1284 | 12196 | 4108 |
| Q8_0 | 80K | f16 | 2048 | 32.75 | 31.99 | 1329 | 14036 | 2268 |
| Q8_0 | 131K | q8_0 | 2048 | 32.40 | 31.68 | 1285 | 13996 | 2308 |
| UD-Q8_K_XL | 80K | q8_0 | 2048 | 25.87 | 25.39 | 1232 | 15218 | 1086 |
| UD-Q8_K_XL | 80K | q8_0 | 4096 | 25.80 | 25.34 | 1141 | 14192 | 2112 |

- TG is weight-bandwidth-bound: KV type and ctx are ~free. **UD-Q8_K_XL is 20% slower** (36% more weight bytes) — eliminated.
- `-ub 4096` costs up to ~2.8 GB VRAM for zero/negative PP gain (dense model; unlike the offloaded MoE) — **`-ub 2048` strictly better**.
- f16 KV works on this hybrid (no 27B-style mandatory-quantized-KV issue on these builds) and is marginally fastest, but q8_0 buys 1–1.8 GB headroom for <1% TG.

### Phase A, MTP cells (draft-mtp, Q8_0 weights)

| ctx | KV | n_max | TG greedy (acc) | TG real (acc) | PP | idle VRAM | headroom |
|---:|---|---:|---:|---:|---:|---:|---:|
| 80K | q8_0 | 2 | **60.03 (86.6%)** | **47.09 (59.9%)** | 1199 | 14555 | 1749 |
| 80K | q4_0 | 2 | 57.29 (80.1%) | 51.34 (70.3%) | 1112 | 15849¹ | 455¹ |
| 80K | q8_0 | 3 | 60.23 (70.3%) | 49.05 (54.1%) | 1199 | 14600 | 1704 |
| 80K | q8_0 | 2 +ub4096 | 59.61 (86.6%) | 46.95 (60.4%) | 995 | 16033 | 271 |
| 131K | q8_0 | 2 | 59.99 (86.6%) | 50.02 (67.3%) | 1211 | 15493 | 811 |
| 80K (UD-Q8_K_XL) | q8_0 | 2 | 46.13 (88.0%) | 34.12 (54.1%) | 1075 | 16153 | 151 |

¹ anomalous first reading; settled to 15060 after probes.

**Headline: no MTP context cliff on the 9B.** Unlike the 27B (speculation silently disables above ~35K ctx / with q8 KV, still true on b9381), the 9B accelerates at 80K *and* 131K with q8 KV: **+85% greedy (60 vs 32.4), +48–58% real-sampler (47–51 t/s; the spread is acceptance noise at temp 1.0)**. n=2 ≥ n=3 on real workloads (acceptance drops faster than payoff — same as the 35B). Real-sampler TG tracks acceptance (~60–75% → 47–55 t/s), consistent with the acceptance-not-VRAM mechanism established in May.

### b9990 vs b9381 — compute flat, footprint dramatically lighter

`llama-bench` gold standard (Q8_0, `-fa 1`, r=3): b9381 pp2048 **1703.6 ± 2.5** / tg128 **26.31 ± 0.01**; b9990 **1684.0 ± 1.3** / **26.54 ± 0.01** → compute is a wash (−1% PP, +1% TG), same conclusion as the b9672 probe.

But the **server VRAM footprint at identical configs is 2.6–3.2 GB lighter on b9990** — and it is real allocation reduction, not deferred/lazy KV growth (VRAM stays byte-flat through a 107K-token prompt, see stress below):

| config | b9381 idle / hr | b9990 idle / hr | Δ |
|---|---:|---:|---:|
| 9B base Q8_0 @80K q8 KV | 12836 / 3468 | **10224 / 6080** | −2612 |
| 9B MTP @80K q8 KV n2 | 14555 / 1749 | **11369 / 4935** | −3186 |
| 9B MTP @131K q8 KV n2 | 15493 / 811 | **12761 / 3543** | −2732 |

b9990 TG/PP on the 9B matches or slightly beats b9381 (MTP greedy 61.4–61.5 vs 60.0–60.2).

### Deep-prompt eviction stress (protocol: fresh shallow TG → deep prompt fills KV → re-measure ×2)

| config | deep fill | fresh → sustained TG | VRAM through stress | verdict |
|---|---|---:|---:|---|
| b9990 MTP @131K q8 n2 | **107,301 tok** @ 579 t/s | 61.32 → **60.97** (−0.6%) | **12763 flat** (hr 3541) | ✅ PASS |
| b9990 MTP @80K q8 n2 | 60,901 tok @ 771 t/s | 61.42 → **60.92** (−0.8%) | 11371 flat (hr 4933) | ✅ PASS |
| b9381 MTP @80K q8 n2 | 60,901 tok @ 763 t/s | 60.08 → **58.75** (−2.2%) | 14551 flat (hr 1752) | ✅ PASS |

### Phase D — the b9990 upgrade question, answered by the production 35B configs

| config on b9990 | fresh TG | deep 61K-tok PP | sustained TG | VRAM | verdict |
|---|---:|---:|---:|---|---|
| 35B **base** Q5_K_XL @80K q8 ot20 **ub4096** (the shipped default) | 29.94 | **367** (vs ~700 on b9381) | **22.65 (−24%)** | 15957 → **14948 during fill** | ❌ **EVICTS** |
| 35B **MTP** Q5_K_XL @80K q4 ot18 ub2048 n2 | 40.71 | 488 | **42.11 (+3.4%)** | 14760–14768 flat (hr 1536) | ✅ PASS (+10% vs 38.3 on b9381) |

The base-default config shows the textbook eviction signature on b9990 (VRAM *drops* ~1 GB during the deep fill as the compute buffer moves to GTT). b9990's allocation rewrite is a big win for the dense-hybrid 9B (−3 GB) and even for 35B-MTP/ub2048 (+10%, stable), but it **regresses the shipped 35B base default** — so **the pin stays at b9381**. (Future lever: a b9990 bump paired with re-tuning the 35B default away from ub4096 — e.g. MTP-as-default at 42 t/s — but that is a separate, deliberate migration, not a free bump.)

### Conclusion & shipped config

- **Shipped (PR): `Qwen3.5-9B-MTP-Q8_0` added to `platform_models.json`** (non-default; switch via `/api/ai/model/switch`): Q8_0 weights, **ctx 81920**, q8_0 KV, `-ub 2048`, draft-mtp n=2, `-ngl 99` — on the pinned b9381: **~59–60 t/s sustained greedy / 47–51 t/s real-sampler / PP ~1200 (deep-fill 763)**, idle 14555 MiB (headroom ~1750), deep-prompt stress PASS. That is **2× the 35B default's 29.1 t/s** and ~1.55× the 35B-MTP option's 38.3, at Q8 numerics with best-precision (q8) KV.
- **UD-Q8_K_XL rejected** (−20% TG, headroom as low as 151 MiB); **ub4096 rejected** (VRAM cost, no dense-model PP gain); **n=3 rejected** (acceptance).
- 131072 ctx is stress-clean on b9990 (hr 3541) but only headroom-marginal on b9381 (hr 811, unstressed) — revisit if/when the build moves.
- **Upstream (b9990) verdict: compute flat, allocator very different** — lighter for dense/ub2048 regimes, evicting for the offloaded ub4096 default. No blind bumps; any move must re-stress every shipped config.

---

## 2026-07-25 — b10107 upgrade probe (latest release) + platform-record E2E

Ran during the hardware-agnosticism E2E on `.58`. Started as "has upstream fixed the b9990/b10032
eviction?" and ended somewhere more useful. **Superseded conclusion below — read to the end before
acting on the first table.** b10107 does reproduce the eviction at `-ub 4096`; what the guarded
re-run then showed is that **the pinned b9381 evicts too, 1-in-2**, and that `-ub 2048` fixes both.

### b10107 vs b9381 — same session, same 51,824-token prompt, shipped default config

Config: `Qwen3.6-35B-A3B-UD-Q5_K_XL` @80K, q8 KV, `-ot blk.20-39`, **`-ub 4096`** — i.e. the
default as shipped, unmodified.

| | b9381 (pinned) | b10107 (latest) |
|---|---:|---:|
| VRAM through the fill | 15406 → **15934** (rises, stable) | 15980 → **14423** (collapses) |
| Sustained TG at depth | **25.60** | 20.80 (−19%) |
| Deep-fill PP | **564.6** | 361.5 (−36%) |
| Verdict | ✅ PASS | ❌ **EVICTS** |

Same textbook signature as b9990/b10032: the compute buffer migrates to GTT partway through the
fill and VRAM *drops* ~1.5 GB instead of growing. **The pin stays at b9381.** Three consecutive
probes (b9990, b10032, b10107) now say the same thing — this is not a transient upstream bug, it is
a durable consequence of the allocator rewrite meeting our `-ot` + `ub4096` regime. Any future bump
has to be paired with re-tuning the 35B default off `ub4096` (MTP-as-default is the standing
candidate), and every shipped config re-stressed. Not a free bump.

### Incidental find: the box was already serving degraded

The pre-test baseline read **24 t/s at idle-VRAM 15330 MiB**, not the documented ~29. The running
llama-server process (up 1h20m) had logged
`Vulkan_Host compute buffer size of 606.7344 MiB, does not match expectation of 1344.4258 MiB` —
a starved compute buffer from an earlier start. A clean restart fixed it (15934 MiB, PP 565).

Worth noting because **`verify_llama_allocation` did not catch this**: its watermark compares
*total VRAM used* (15330) against `min_healthy_vram_mb` (15000) and passed, while the actual
starvation was in the compute buffer, which that number does not see. The real signal is the
journal line above. A watermark on total VRAM is a proxy, and this is the case where the proxy
fails — see the follow-up note in `docs/plans/HARDWARE_AGNOSTIC.md` §8.

### Guarded re-run — the first table was contaminated, and the conclusion changes

The A/B above ran via the systemd unit, but the follow-up hardening sweep did not, and **two
orphaned `llama-server` processes were competing for VRAM** through part of it (a backgrounded ssh
was SIGHUP'd and left children behind). Every result from that sweep was discarded. The harness now
enforces two guards, both of which the production unit already had and the harness did not:

1. drain VRAM below 300 MiB before each start (mirrors the unit's `ExecStartPre` VRAM-settle gate);
2. abort unless **exactly one** `llama-server` is running.

Re-measured under those guards — same 51,824-token fill, same flags otherwise, ctx 81920, q8 KV:

| build | `-ot` | `-ub` | idle | headroom | TG fresh → post-fill | fill PP | PP@2k | verdict |
|---|---:|---:|---:|---:|---|---:|---:|---|
| b9381 | 20 | 4096 | 15406 | 898 | 28.78 → 28.66 | 564.6 | 758.3 | PASS |
| b9381 | 20 | 4096 | 15406 | 898 | 28.64 → **22.21** | 498.7 | — | ❌ **EVICTS** |
| b9381 | 20 | 2048 | 15635 | 669 | 28.68 → 28.52 | 619.1 | 496.5 | PASS (3/3) |
| b9381 | 19 | 4096 | 16121 | 183 | 27.93 → 27.91 | 624.4 | — | PASS |
| b9381 | 18 | 4096 | 15559 | 745 | 27.32 → 27.39 | 619.7 | — | PASS |
| b10107 | 20 | 4096 | 15948 | 356 | 29.00 → **22.47** | 365.2 | 781.5 | ❌ EVICTS |
| **b10107** | **20** | **2048** | **15197** | **1107** | **29.12 → 28.97** | **625.0** | **617.7** | ✅ **PASS (2/2)** |
| b10107 | 18 | 2048 | 14073 | **2231** | 28.07 → 27.84 | 607.5 | — | ✅ PASS |

**The shipped default was never stable.** `b9381 + ot20 + ub4096` evicts on ~half of starts *on the
pinned build*, from a byte-identical starting allocation (idle 15406 both times) — the divergence
happens during the fill, when the compute buffer either stays put (VRAM → 15935) or relocates to
GTT (VRAM → 14929). Three earlier probes read this as "newer builds broke our config"; it is more
accurately "our config was a coin flip, and newer allocators lose the flip more often."

`-ub 2048` is deterministic where `ub4096` is not: identical idle *and* identical post-fill VRAM
across repeats, on both builds. The micro-batch sizes the compute buffer, and that buffer's
relocation was the entire failure mode — so this is the lever that matches the mechanism, which is
why extra `-ot` offload only helps sideways (it does stabilise `ub4096`, at 2–4% TG, by shrinking
what has to fit around the buffer rather than shrinking the buffer).

### Shipped (2026-07-25): pin → b10107, default → `-ub 2048`

Versus the old default's **good** runs: sustained TG 28.66 → 28.97, headroom 898 → 1107 MiB,
deep-fill PP 565 → 625 — and the 50% eviction risk removed. Versus its *expected* value (half its
runs at 22.2 t/s) the gain is far larger. The single cost is shallow-prompt **PP@2k 758 → 618
(−19%)**; b10107 recovers most of what `ub2048` gives up there (496 on b9381 → 618 on b10107),
which is why the build bump and the micro-batch change ship together rather than separately.

Conservative alternative if KV growth is the worry: `-ot blk.18-39` with `-ub 2048` on b10107 —
**2231 MiB headroom** at ~−4% TG.

Unmeasured / caveats: n=2–3 per cell, single session, one prompt shape. The eviction is stochastic,
so "PASS 2/2" bounds the failure rate loosely, not tightly. Whisper was on CPU throughout.

---

## 2026-08-10 — Muse Glimmer 30B (new arch, same 16 GB card)

Meta's Muse Glimmer 30B dropped 2026-08-09 and Unsloth's GGUFs the morning after.
It is the first model evaluated here that is **dense** rather than MoE, and the
first with a vision tower. Swept on .58 the same day.

**Upstream support was master-only for one day.** `muse-glimmer` merged as PR
#26841 on 2026-08-10 11:07 UTC, after b10344 had already been cut ~5 commits
earlier — so on the day of the sweep no release tag contained the arch and the
pinned b10107 binary could not load the file at all. Everything below was
therefore measured on a source build of master `689e227` (Vulkan) staged at
`~/llama.cpp-glimmer`. Building it needed `spirv-headers`, which
`_build_llamacpp_from_source` does not install.

**Resolved 2026-08-11: the arch reached the releases at b10356**, and the pin is
now **b10361**. That build was re-measured against the master build every number
here was taken on, and it is identical within noise — idle **15968 vs 15969 MiB**,
TG **18.79 vs 18.83**, PP@2k **480.0 vs 479.5**, coherent output. So the results
below stand on the shipped binary; the master worktree is no longer needed.

### Why the whole 131072 window fits in 16 GB

| | |
|---|---|
| Layers | 52 — only **13 full-attention**, 39 sliding-window (2048) |
| Attention | GQA 32 Q / **2 KV** heads, head_dim 128 |
| Position | RoPE θ=500,000 on local layers only; global layers are NoPE |

Two KV heads and a 2048 window on three quarters of the layers make the cache
almost free — the step from ctx 81920 to 131072 costs only ~370 MiB. That is the
opposite of the 35B's profile and it is why context, not KV type, turned out to
be the cheap axis here.

### Fit sweep — UD-Q3_K_XL (13.36 GB) vs UD-Q4_K_XL (15.88 GB)

TG is flat across every config: the model is bandwidth-bound, so quant size sets
throughput and nothing else moves it. All cells `-ngl 99 -fa on -t 6`.

| quant | ctx | KV | `-b`/`-ub` | TG | PP@2k | idle | headroom |
|---|---:|---|---:|---:|---:|---:|---:|
| Q3_K_XL | 81920 | q8_0 | 4096/2048 | **21.61** | 425.6 | 13286 | **2977** |
| Q3_K_XL | 131072 | q8_0 | 4096/2048 | 20.80 | 271.8 | 13653 | 2647 |
| Q3_K_XL | 131072 | q8_0 | 4096/4096 | 21.54 | 425.0 | 15072 | 1231 |
| Q4_K_XL | 81920 | q8_0 | 4096/2048 | 18.83 | 477.9 | 16018 | 245 |
| Q4_K_XL | 81920 | q8_0 | 2048/1024 | 18.82 | 479.8 | 15540 | 705 |
| Q4_K_XL | 98304 | q8_0 | 2048/1024 | 18.82 | 479.6 | 15683 | 561 |
| Q4_K_XL | 114688 | q8_0 | 2048/1024 | 18.83 | 479.5 | 15825 | 420 |
| **Q4_K_XL** | **131072** | **q8_0** | **2048/1024** | **18.83** | **479.5** | **15969** | **335** |
| Q4_K_XL | 131072 | q4_0 | 2048/1024 | 18.82 | 474.9 | 15524 | 780 |

Q4 costs **−13% TG** against Q3 and buys **+13% PP** and a quantization tier
(the model card puts its K-Quant-17GB at 1.0% degradation; Q3_K_XL sits below
even that). `-ub 1024` is the lever that makes Q4 viable — it shrinks the
compute buffer for ~460 MiB at identical throughput, the same mechanism the
2026-07-25 entry identified.

**Lowering context does not buy headroom here.** Q4 from 131072 down to 16384
recovers only 738 MiB, because the weights dominate and the KV is tiny. There is
no context to retreat to; either the quant fits or it does not.

### Eviction gate at ctx 131072

Both KV types were run three times from cold plus a 108,292-token prompt.

| KV | 3× idle | post-108K | PP@108K | TG after fill | verdict |
|---|---:|---:|---:|---:|---|
| q8_0 | 15969 / 15969 / 15969 | 16029 (**275**) | 287.6 | 18.81 | PASS 3/3 |
| q4_0 | 15524 / 15524 / 15524 | 15584 (**720**) | 305.1 | 18.78 | PASS 3/3 |

Byte-identical idle across repeats, ~60 MiB of buffer growth under a full-depth
fill, zero Vulkan allocation warnings, and TG returning to its shallow rate —
neither evicts. **Shipped: q8_0**, for KV numerics over a 128K window. The
reservation is upgrade margin, not today's behaviour: 275 MiB is just above the
~250 MiB knee, and b9672 once idled +545 MiB at an identical config. If a future
build tips it, q4_0 holds the same window at 720 MiB for ~1% TG.

### DFlash speculative decoding — rejected on this card

The model ships a block-diffusion drafter (`dflash-kquant.gguf`, 16-token blocks,
`--spec-type draft-dflash`) that the card measures at **3.1× on an RTX 5090**.
On 16 GB it is a large loss:

| config | TG | PP@2k | idle | headroom | acceptance |
|---|---:|---:|---:|---:|---:|
| baseline, ctx 81920 | **21.61** | **425.6** | 13286 | 2977 | — |
| + DFlash n_max 16 | 15.32 | 39.4 | 15566 | 738 | 21.1% |

The drafter costs ~2.3 GiB. PP collapsing 10× while TG only drops 29% is the
memory-pressure signature, not a drafting-quality one — and the cost is
independent of `--spec-draft-n-max`, so smaller draft sizes cannot recover it.
The card targets a 24 GB envelope for weights + KV + encoder + drafter
simultaneously; that is the hardware this feature wants.

### Behaviour notes

- **`reasoning_strength` defaults to `high`.** It is a chat-template variable
  (`low`/`medium`/`high`/`xhigh`), set via `--chat-template-kwargs`, and rendered
  into the system prompt as `Reasoning strength: <value>.` At `high` the model
  spent ~900 reasoning tokens on a three-sentence question — ~40 s before the
  first visible word at 18.8 t/s. The card recommends high/xhigh for agentic
  work; `medium` is the better default for interactive chat.
- **Tool calls work natively.** llama.cpp parses the ATEM markup
  (`<atem:invoke>`) into OpenAI-shaped `tool_calls`; verified end to end with a
  namespaced `homeassistant.set_light` returning `{"room":"kitchen","brightness":30}`.
- **Vision is untested.** An mmproj is published (2.05 GB Q8_0 / 1.40 GB kquant)
  but was not loaded — at 335 MiB headroom there is nowhere to put it, so vision
  needs Q3_K_XL or a smaller context.

Unmeasured / caveats: one session, one prompt shape, n=3 per gated cell. TG
figures are greedy 200–256-token completions. Quantization *quality* was not
measured — the Q3-vs-Q4 quality claim is the model card's, not ours. Whisper was
`--no-gpu` throughout, so none of this headroom is shared with speech.

## 2026-08-14 — Qwen3.8-27B IQ4_XS (supersedes the Qwen3.6-27B entry)

Catalog file as of 2026-08-20 is **UD-IQ4_XS** (Unsloth Dynamic V3,
14,252,845,984 bytes / 13,592 MiB). Unsloth withdrew the IQ4_XS GGUF the
measurements below were taken on (that URL now 404s). Re-measured 2026-08-20:
q4_0 reaches **131072**; shipped is **q8_0 @ 81920**. See the 2026-08-20
sections. The tables below are the IQ4_XS numbers.

`unsloth/Qwen3.8-27B-GGUF` at IQ4_XS — 15,705,861,088 bytes (14,978 MiB) against
16,304 MiB usable, on the pinned **b10361**. Arch is `qwen35`, the same
Gated-DeltaNet hybrid as the Qwen3.6-27B it replaces, so the DeltaNet rules carry
over unchanged: `-ngl 99`, no `-ot`, no `--no-kv-offload`, and `-fa` with an
explicit `--cache-type-k/v` (mandatory on AMD Vulkan).

**Shipped: ctx 81920, q4_0 KV, `-b 2048 -ub 1024`, 17.14 t/s TG / 438.7 PP@2k.**

### Why KV type is the context lever here

GGUF metadata: `block_count 65` (64 + 1 MTP), `full_attention_interval 4`,
`head_count_kv 4`, `key_length`/`value_length` **256**, native
`context_length 262144`.

Only 16 of 64 layers are full-attention, but those 16 carry a fat cache —
4 KV heads × 256 head_dim × 2 (K+V) × 16 layers = **32,768 values per token**:

| KV type | per token | 32768 | 49152 | 65536 | 81920 |
|---|---:|---:|---:|---:|---:|
| q8_0 | 34.0 KiB | 1088 MiB | 1632 MiB | 2176 MiB | 2720 MiB |
| q4_0 | 18.0 KiB | 576 MiB | 864 MiB | 1152 MiB | 1440 MiB |

Measured: q8_0 minus q4_0 at ctx 32768 was **+512 MiB** (15,661 vs 15,149) —
+16 KiB/token, the theoretical delta to the byte.

| q8_0 ctx | idle | headroom | TG | PP@2k |
|---:|---:|---:|---:|---:|
| 32768 | 15661 | 643 | 17.18 | 456.9 |
| 49152 | 16237 | **67** | crash | — |
| 65536 | 16135 | 169 | 11.43 | 69.5 |

**8-bit KV caps this model between 32K and 49K — roughly half the q4_0 ceiling.**
q4_0 is what makes 81920 reachable; it costs context, it does not save it.

### Degradation is silent, and shallow probes miss it

VRAM pins at **~16,110 MiB** for every ctx ≥ 81920 and stays there. Past that the
driver spills to GTT instead of failing, so the server keeps answering — slower.
A 200-token completion never touches the deep cache, so shallow TG reports full
speed well past the point the cache stops fitting.

Fit ladder, q4_0, `-b 2048 -ub 1024`, `-t 6`:

| ctx | idle VRAM | headroom | TG | PP@2k | note |
|---:|---:|---:|---:|---:|---|
| 32768 | 15149 | 1155 | 17.29 | 453.8 | |
| 49152 | 15469 | 834 | 17.17 | 439.7 | |
| 57344 | 15629 | 675 | 17.18 | — | |
| 65536 | 15640 | 664 | **14.91** | — | value-specific dip, see below |
| 73728 | 15949 | 355 | 17.17 | — | |
| **81920** | **16109** | **195** | **17.15** | **438.7** | **shipped** |
| 98304 | 15973 | 331 | 12.60 | — | spilling |
| 114688 | 16111 | 193 | 11.41 | 70.8 | spilling: PP −84% |
| 131072 | 16112 | 192 | crash | — | dies on first request |

### Eviction gate — the deciding test

Protocol: fresh shallow TG → prompt filling ~92% of the window → re-measure ×2.
Eviction signature is VRAM dropping and staying down while shallow TG collapses.

| ctx | headroom | fill | fill PP | TG fresh → sustained | VRAM through fill | verdict |
|---:|---:|---:|---:|---|---|---|
| 49152 | 834 | 43,704 | 329.3 | 17.17 → 17.15 | 15470 flat | PASS |
| 57344 | 675 | 50,983 | 312.2 | 17.18 → 17.13 | 15630 flat | PASS |
| 65536 | 664 | 58,262 | 296.4 | 14.91 → 14.89 | 15640 flat | PASS, −13% TG |
| 73728 | 355 | 65,541 | 283.2 | 17.17 → 17.14 | 15949 flat | PASS |
| **81920** | **194** | **72,849** | **270.0** | **17.15 → 17.14** | **16110 flat** | **PASS — max** |
| 98304 | 330 | aborted | 65 | 12.60 | — | degraded, not run to completion |

Nothing evicts at or below 81920 — VRAM is byte-stable across a full-depth fill
and TG returns to its shallow rate. 81920 holds full throughput on only 194 MiB
of headroom, below the ~250 MiB knee seen on Muse Glimmer; that leaves little
margin for a heavier future build, and 73728 (355 MiB) is the conservative
fallback at identical throughput.

**Oddity worth recording: ctx 65536 specifically is slow.** It runs 14.91 t/s
while both 57344 and 73728 hold 17.17 — not a monotonic ceiling, a
value-specific dip. The Qwen3.6-27B measured 14.93 t/s at 64K on this same card,
so it reproduces across two models of this architecture. Avoid 65536.

### Micro-batch

At ctx 49152, q4_0:

| `-b`/`-ub` | idle VRAM | headroom | TG | PP@2k |
|---|---:|---:|---:|---:|
| 2048/512 | 15110 | 1194 | 12.68 | 99.2 |
| **2048/1024** | 15469 | 835 | **17.17** | **438.9** |
| 4096/2048 | 15831 | 473 | 17.14 | — |
| 4096/4096 | 16117 | 187 | 15.32 | 236.3 |

`-ub 512` frees 359 MiB (≈20K more q4_0 tokens) but costs 26% TG and 77% PP — a
bad trade even when context is the priority.

### MTP — the throughput alternative, rejected for context

This GGUF ships its MTP block (`blk.64.nextn.*`), reported
`unused -- ignoring` until `--spec-type draft-mtp` is passed. It works, at ~68%
draft acceptance (mean draft length 2.36–2.39), and costs ~684 MiB:

| config | idle | headroom | TG | PP@2k |
|---|---:|---:|---:|---:|
| ctx 32768 + MTP n2 | 15833 | 471 | **30.37** | 403.3 |
| ctx 40960 + MTP n2 | 16025 | 279 | **30.42** | 402.8 |
| ctx 49152 + MTP n2 | 16217 | 87 | 30.41 | 394.6 |
| ctx 81920 + MTP n2 | 16242 | **62** | 19.49 | **91.0** |

**+76% TG** against the 17.29 baseline — the same magnitude the Qwen3.6-27B got
from MTP. But the memory it needs comes straight out of the context budget: at
81920 headroom falls to 62 MiB and PP collapses 438 → 91. `ctx 40960 +
--spec-type draft-mtp --spec-draft-n-max 2` is the max-throughput operating
point (30.42 t/s at 276 MiB headroom). **Max context was chosen over max
throughput**; switch to the 40960 MTP config if that preference ever inverts.

### Two things that free nothing

- **The MTP tensors are already not resident.** The whole `blk.64` block
  (~265 MB) loads only when `--spec-type draft-mtp` is passed; otherwise
  llama.cpp lists every tensor as `unused -- ignoring`. Offloading them to CPU
  frees 0 MiB.
- **There are no vision tensors in this file.** Grep for `v.blk`/`vision`/
  `mmproj` in the load log returns 0. The image encoder is a separate
  `mmproj-*.gguf` this entry never loads, so vision costs nothing today — and
  enabling it would *add* 1–2 GB, spending context rather than saving it.

### Reasoning effort — the kwarg name differs from Muse Glimmer

**This template reads `reasoning_effort`, not `reasoning_strength`.** Glimmer's
variable is `reasoning_strength` (`low`/`medium`/`high`/`xhigh`); Qwen3.8's is
`reasoning_effort` and its levels are `xhigh`/`medium`/`low` — no `high`.
Copying Glimmer's kwarg across would not error: `--chat-template-kwargs` puts
unknown keys into the Jinja context, the template never reads them, and the
setting is **silently inert**. Verified against the published
`tokenizer_config.json`, whose template resolves
`reasoning_effort|default('xhigh')`.

So the model was already running at xhigh by template default. Shipping
`--chat-template-kwargs '{"reasoning_effort": "xhigh"}'` explicitly (2026-08-15)
pins that against an upstream default change and makes the intent visible in the
unit file; it is not a behaviour change on its own.

### Single-generation cap (`max_tokens` 16384)

**Raised to a flat 16384 on all three models (2026-08-15)** to leave room for
multi-hour agentic turns; the per-model 8192/12288 split below is history. The
OpenClaw-side ceiling counts **reasoning tokens as well as answer tokens**, so
it has to clear whatever the model spends thinking:

| model | think backstop | cap (was) | basis |
|---|---|---:|---|
| Muse Glimmer 30B | none | 16384 (8192) | measured: max 7302 over n=3860 generations at xhigh |
| Qwen3.8-27B | none (`--reasoning-preserve` only) | 16384 (12288) | unmeasured; xhigh with no server-side think cap |
| Qwen3.6-35B-A3B | `--reasoning-budget 8192` | 16384 (12288) | 8192 think + the rest for the answer |

The 35B is the sharp case: with a flat 8192 cap, a turn that spends its full
reasoning budget hits the ceiling at the instant thinking closes and has **zero
tokens left for the answer or tool call** — precisely on the runaway-`<think>`
turn the budget exists to contain.

Truncation is not graceful degradation. A step cut mid-tool-call emits malformed
JSON and fails the turn outright rather than shortening it, so these are sized
above the observed tail, never at it. The cap stays coupled to
`timeoutSeconds` (1800) in `patch_openclaw_config` — worst case is a full-depth
prefill plus a full-length completion, and it must fit that ceiling:

| model | prefill | generation | total | of 1800 s |
|---|---:|---:|---:|---:|
| Muse Glimmer 30B | 456 s | 1024 s | 1480 s | 82% |
| Qwen3.6-35B-A3B | 303 s | 964 s | 1267 s | 70% |
| Qwen3.8-27B | 187 s | 964 s | 1151 s | 64% |

**Past ~24k tokens a single Glimmer generation would outlive the HTTP timeout**
and convert a slow call into a failed turn. Re-measure both before raising
either. 16384 is 2.2x the largest generation ever observed here, so it is
headroom, not a response to measured truncation.

### Harness note

Early passes produced scattered 0 t/s "failures" that did not reproduce
standalone (a config flagged as crashing ran at 17.14 t/s when started by hand).
Two causes, both fixed in `bench-qwen38-27b.sh`: `/health` flips to OK before the
server can serve, so cells were measured mid-allocation; and the drain between
cells waited on VRAM only, letting a dying server keep port 8099 and answer the
next cell's health check. Readiness is now gated on a real completion and the
drain waits for process exit plus port release. Any 0 t/s row in an older
`q38-*.out` is harness noise, not a model result.

Unmeasured / caveats: one session, one prompt shape. TG figures are greedy
200-token completions; the deep-fill PP figures are single runs. 98304 was
aborted mid-fill once degradation was unambiguous rather than run to a verdict.
Contexts between 81920 and 98304 (e.g. 90112) were not tested. Quantization
*quality* was not measured. Whisper was `--no-gpu` throughout.

## 2026-08-16 — Tool escalation under a broken `web_search`: Glimmer vs Qwen3.8-27B

An unplanned A/B on the production box: the same multi-source web-research task
was given to Muse Glimmer 30B **UD-Q4_K_XL** (2026-08-15) and to Qwen3.8-27B
**IQ4_XS** (2026-08-16) — both the shipped catalog files, on the pinned b10361 —
while `web_search` had **no provider configured at all** — no
search key exists anywhere in `~/.openclaw/openclaw.json`, so every `web_search`
call returns
`{"status":"error","error":"web_search is disabled or no provider is available."}`.
The `browser` plugin was enabled for both runs.

Glimmer produced a correct, sourced answer. Qwen3.8-27B produced nothing and
burned 11 minutes doing it. **The missing provider is not what separates them** —
both models detected the failure and both pivoted to fetching. What differs is
*what they fetched*.

### The finding — SERP-scraping vs driving the browser

**Qwen3.8-27B: all six `web_fetch` calls went to a search-engine result page.**
Result sizes, one per call:

```
7057  https://html.duckduckgo.com/html/?q=<query 1>
7046  https://html.duckduckgo.com/html/?q=<query 2>
6008  https://html.duckduckgo.com/html/?q=<query 3>
6029  https://html.duckduckgo.com/html/?q=<query 4>
6005  https://html.duckduckgo.com/html/?q=<query 5>
6086  https://html.duckduckgo.com/html/?q=<query 6>
```

It never fetched a content site, never followed a link, and never escalated to
the `browser` tool that was available to it. It got ~6 KB of ad markup each time
and said so in its own reasoning: *"DuckDuckGo's HTML search is being heavily
polluted with ads, and the actual organic results aren't visible in the truncated
output."* Having recognised the strategy was failing, it repeated it five more
times.

**Glimmer escalated on the first failure.** It opened a real browser over CDP,
ran the search there, read *rendered* results, then fetched the specific articles
it found:

```
649    browser: cdp transport, running
358    browser → <search engine> results page
8976   (rendered page content)
31762  (rendered page content)
1674   <trade publication — article page>
2383   <trade publication — article page>
1902   <manufacturer site>
```

Cadence differed too. Glimmer made **13 model calls with one tool call each**,
14–40 s of thinking apiece, and answered in ~6.5 min. Qwen batched 3–4 parallel
fetches per call and then, on its fourth call, emitted **zero** tool calls.

| | Glimmer 30B UD-Q4_K_XL | Qwen3.8-27B IQ4_XS |
|---|---|---|
| `web_search` failures | 1 | 3 |
| escalated to `browser` | yes, immediately | never |
| real content pages fetched | 3 | 0 |
| model calls | 13 | 4 |
| outcome | sourced answer, ~6.5 min | no answer, ~16.5 min |

### The runaway `<think>` loop — and why it contradicts a shipped assumption

Ungrounded, Qwen3.8-27B fell back to reciting candidates from parametric memory
and latched. The fourth model call produced an 18,731-char thinking block in
which a single 6-token list item — `- **"<name>"?**` — repeats **1,116 times**:

```
prompt eval  53013 ms / 13322 tokens (251.29 t/s)
eval        649145 ms /  8536 tokens ( 13.15 t/s)
total       702159 ms / 21858 tokens
```

It then exited thinking, wrote one line announcing the next research step it
intended to take — and **ended the turn with no tool call**. No error, no
timeout, no crash: the turn completed normally and the session went idle, so from
the chat side it looks like work still in progress. GPU load of 0 is the tell
that it is not.

This is the failure mode the **Sampler tuning** section above exists to prevent —
*"the A3B MoE variants are documented to fall into infinite `<think>` loops on
tool-call failures"* — and it names the trigger exactly: a tool-call failure.
That section also records the decision that *"the 27B IQ4_XS entry (DeltaNet
hybrid, dense Qwen3.6-27B) is left at `presence_penalty=0` per its own model
card."* **That assumption is now falsified for the Qwen3.8-27B entry.** The
dense-card guidance held right up until a tool failure left the model with
nothing to condition on.

The Qwen3.8-27B entry is the only model in the catalog with **both** no
repetition penalty and no server-side think cap:

| | 35B-A3B UD-Q5_K_XL | 3.8-27B IQ4_XS | Glimmer 30B UD-Q4_K_XL |
|---|---|---|---|
| `--presence-penalty` | 1.5 | **0.0** | *(unset → 0.0)* |
| `--reasoning-budget` | 8192 | **absent** | absent |
| `--top-k` | 20 | 20 | 64 |

This also fills in the `Qwen3.8-27B ... unmeasured` row in the single-generation
cap table above: a runaway turn on this entry is now measured at **8,536 decoded
tokens**, under the 16384 `max_tokens` ceiling, so `max_tokens` did not and would
not have contained it.

### Do not "fix" this by copying Glimmer's `top-k`

`top-k 20` is the Qwen card's value and is consistent across both Qwen entries;
`64` is Glimmer's. The two are tuned to different logit distributions and are not
interchangeable. More to the point, widening the sampling pool does not teach a
model to escalate to the browser — the loop is a *symptom* of having no grounded
facts, not the cause of the failed research. The likely effect of a wider pool
here is to convert a visibly broken turn into a fluent, confident, hallucinated
answer, which on a task whose premise was a hard numeric safety constraint is the
worse failure.

If the entry is retained, the defensible change is to align it to the **35B-A3B**
precedent — `--presence-penalty 1.5` plus a `--reasoning-budget` — not to
Glimmer's sampler. Note that at the **13.15 t/s** observed here even an 8192
think cap is still ~10 min of wall clock; a meaningful bound on this model needs
roughly 4096.

### Incidental

- Observed TG was **13.15 t/s** at a 56.5k-token depth against the **17.14 t/s**
  benchmarked for this config — ~23% under, unexplained, worth a separate look.
- `platform_models.json` doc drift: the Qwen3.8-27B benchmark note says *"that is
  why its max_tokens is 12288"*, but the field is `16384` (and `openclaw.json`
  agrees at 16384).

Unmeasured / caveats: n=1 per model, and **the two prompts were not byte
identical** — one carried an additional numeric constraint the other did not.
Same task shape and same broken-`web_search` conditions, but this is a field
observation, not a controlled eval. **Quantization**, sampler settings and
context window also differed between the runs — IQ4_XS at 81920/q4_0 KV versus
UD-Q4_K_XL at 131072/q8_0 KV — so tool-escalation behaviour is confounded with
all three. Quantization damage is a live hypothesis for the degenerate loop
specifically and was not tested; nothing here isolates escalation to the weights
alone, and no claim is made that either quant is the better-preserved one. No repeat runs were attempted — the box was switched back to
Glimmer as the catalog default before further testing. Prompt text, queries and
fetched URLs are deliberately omitted.

## 2026-08-20 — Qwen3.8-27B UD-IQ4_XS remeasure: ctx 81920 → 131072

The Dynamic V3 file is 1,386 MiB smaller than the withdrawn IQ4_XS
(13,592 vs 14,978 MiB). That saving is real VRAM, not just disk: at the
old shipped ctx 81920, idle VRAM dropped 16109 → **14803** (headroom
**1501 MiB**, up from 195). Fit ladder on the same flags (q4_0 KV,
`-b 2048 -ub 1024`, `-t 6`, whisper `--no-gpu`, b10361):

| ctx | idle VRAM | headroom | TG | PP@2k | IQ4_XS at this ctx |
|---:|---:|---:|---:|---:|---|
| 81920 | 14803 | **1501** | 18.84 | 420.1 | 16109 / 195 hr / 17.15 t/s |
| 98304 | 15123 | 1181 | 18.84 | 462.0 | spilled, 12.60 t/s |
| 114688 | 15443 | 861 | 18.83 | 462.2 | spilled, 11.41 t/s / PP 70.8 |
| **131072** | **15763** | **541** | **18.83** | **461.7** | crashed on first request |

VRAM steps by a flat **320 MiB per 16K tokens** (18 KiB/token q4_0 KV,
to the byte). No pin-at-ceiling, no GTT spill.

Eviction at 131072: 116,552-token fill @ 213.3 t/s, VRAM **byte-stable
15765**, TG 18.83 → 18.63 (−1.1%) **PASS**. Shallow TG at 81920 is
18.84 vs the IQ4_XS 17.14 — extra headroom, not a different kernel.

**q4_0 pick: ctx 131072**, `min_healthy_vram_mb` 15000. MTP at 131072 was
not re-run: it costs ~684 MiB and would consume the 541 MiB headroom.
Worst-case OpenClaw turn at this ctx is 131072/213.3 + 16384/18.8 ≈ 1485 s.
Superseded the same evening by q8_0 @ 81920 (below).

## 2026-08-20 — Qwen3.8-27B UD-IQ4_XS q8_0 KV @ 81920

Same file, same flags otherwise, KV q8_0. Fit:

| ctx | idle VRAM | headroom | TG | PP@2k | verdict |
|---:|---:|---:|---:|---:|---|
| 57344 | 15219 | 1085 | 18.82 | 456.5 | fits |
| 73728 | 15795 | 509 | 18.82 | 462.7 | fits |
| **81920** | **16083** | **221** | **18.82** | **461.7** | **PASS** |
| 98304 | 16053 | 251 | 12.09 | 63.4 | spill |

Idle matches the q4 ladder + 16 KiB/token to the MiB. 221 MiB is under
the Glimmer ~250 MiB knee; eviction still held: 72,849-token fill @
266.3 t/s, VRAM **byte-stable 16085**, TG 18.81 → 18.76 (−0.3%).

**Shipped: ctx 81920, q8_0 KV**, `min_healthy_vram_mb` 15500. Same 18.8 t/s
as q4 @ 131072 — KV quality, not speed. q4 @ 131072 remains the
max-context alternative (half the KV bits, 60% more window). Worst-case
OpenClaw turn is 81920/266.3 + 16384/18.8 ≈ 1179 s (66% of the 1800 s
ceiling).

## 2026-09-26 — Arc Pro B60, Qwen3.8-27B UD-Q4_K_XL on SYCL

Card: Intel Arc Pro B60 (xe, 23.9 GiB usable), Ryzen 5 5600. File:
`Qwen3.8-27B-UD-Q4_K_XL.gguf`, 17,559,178,144 bytes. Flags unless noted:
`-ngl 99 -fa on`, q8_0 KV, no `-ot`, no `-nkvo`. Vulkan numbers are the
pinned b10361 binary. SYCL is llama.cpp master `2145525`, built with
oneAPI 2025.3, `GGML_SYCL_F16=ON`, Level Zero 26.35 unpacked beside the
distro driver (Ubuntu's 26.05 libze aborts on this card).

Vulkan Q5 on the same box was about 4.4 t/s decode. Vulkan Q4 is the
baseline below. SYCL Q4:

llama-bench, `-b 4096 -t 6 -r 1`. Decode does not move. Prefill does.

| ubatch | pp512 | pp2048 | pp4096 | tg128 |
|---:|---:|---:|---:|---:|
| 512 | 552 | 523 | 484 | 15.50 |
| 1024 | 532 | 645 | 590 | 15.50 |
| 2048 | 532 | 738 | 666 | 15.50 |
| 4096 | 531 | 737 | 705 | 15.50 |

Threads 4 / 6 / 12 at `-b 2048 -ub 1024` all decode at 15.5 t/s
(pp2048 645–661). The same Vulkan sweep on b10361 sat at 7.3–7.5 t/s
decode and 256–273 t/s pp2048.

Server, ctx 131072, greedy, the same 64-token and 813-token prompts
used for the Vulkan check:

| backend | -b / -ub | TG | PP (813 tok) | VRAM | GTT |
|---|---:|---:|---:|---:|---:|
| Vulkan b10361 | 2048 / 1024 | 7.7 | 244 | 20.3 GiB | 299 MiB |
| SYCL 2145525 | 2048 / 1024 | 16.1 | 511 | — | — |
| SYCL 2145525 | 4096 / 2048 | 16.1 | 498 | 21.3 GiB | 644 MiB |

`-b 4096 -ub 2048` is the shipped Arc point: it is the pp2048 peak, it
fits at ctx 131072 with no device-fit warning and about 2.6 GiB of VRAM
left, and decode matches every other batch. `-ub 4096` was the pp4096
peak in llama-bench (705 vs 666) and was not fit-checked at 131072.
Both fused Gated DeltaNet paths (autoregressive and chunked) are on by
default in this master build; the run did not log an unsupported-op
error or a CPU device mismatch.

Q5_K_XL was not re-run on SYCL. Its profile keeps `-b 2048 -ub 1024` because
the file is 3 GiB heavier and the 21.3 GiB reading is the Q4 file.
UD-Q5_K_M was the next B60 standard; Glimmer Q5 replaces it at the end of
this file.

## 2026-09-26 — Arc Pro B60 standard: Qwen3.8-27B UD-Q5_K_M

`Qwen3.8-27B-UD-Q5_K_M.gguf`, 19,771,509,664 bytes (18.40 GiB). Same SYCL
build and the same `-b 4096 -ub 2048 -t 6 -ngl 99 -fa on` as the Q4
point. Native context is 262,144. Batch and thread sweeps were not
repeated: on the Q4 sibling, decode stayed at 15.5 t/s across ubatch
512–4096 and threads 4/6/12.

| KV | ctx | TG | PP (813 tok) | VRAM | GTT | fit warning |
|---|---:|---:|---:|---:|---:|---|
| q8_0 | 131072 | 16.95 | 520 | 23.4 GiB | 33 MiB | yes |
| q4_0 | 131072 | 17.06 | 538 | 21.4 GiB | 630 MiB | no |
| q4_0 | 196608 | 16.78 | 515 | 23.0 GiB | 32 MiB | yes |
| q4_0 | 262144 | — | — | — | — | does not finish loading |

llama-bench at q8, `-b 4096 -ub 2048`: pp512 579, pp2048 743, pp4096 691,
tg128 16.79. KV type does not move decode. q4_0 reaches ctx 196608 at
the same speed. **Shipped for the B60: ctx 131072, q8_0 KV.** 262144
does not come up.

`mmproj-F16.gguf` (927,607,488 bytes, 885 MiB) loads on the shipped
q8 / 131072 config. The server reports the multimodal model loaded.
Text holds: decode 16.3 t/s (16.95 without it), 813-token prefill 487
(520 without it). Resident VRAM goes from 23.4 GiB to 23.6 GiB, and
GTT from 33 MiB to 919 MiB. The projector does not fit in the remaining
VRAM; that 885 MiB sits in GTT. It is not part of the shipped flags.
No quantized mmproj is published, only F16 and BF16. An image was not run.

## 2026-09-26 — Arc Pro B60 standard: Muse Glimmer 30B UD-Q5_K_M

`Muse-Glimmer-30B-UD-Q5_K_M.gguf`, 19,194,274,848 bytes (17.86 GiB). Same SYCL
build as the Qwen point: llama.cpp 2145525, `-ngl 99 -fa on -t 6 -b 4096 -ub 2048`,
q8_0 KV. Sampler stays Glimmer's (`top-k 64`, `reasoning_strength` xhigh).
Trained context is 131072. Global layers are NoPE and the other layers are a
2048 sliding window, so a longer window is cheap in KV and is a memory fit,
not a quality measurement. The server logs a training-context overflow warning
above 131072.

llama-bench at ctx fitting the prompt, q8: pp512 ~760, pp2048 905, pp4096 ~790,
tg128 19.85. ubatch 1024 drops pp2048 to 849. ubatch 2048, 4096, and 8192 match.
Threads 4/6/12, batch 2048/8192, and q4_0 KV all decode at 19.8 t/s.

Server, greedy, the same short prompts:

| ctx | TG | PP (814 tok) | VRAM | GTT | fit warning | VRAM left |
|---:|---:|---:|---:|---:|---|---:|
| 131072 | 19.8 | 626 | 19.2 GiB | 684 MiB | no | 4.7 GiB |
| 262144 | 19.6 | 576 | 20.6 GiB | 1.17 GiB | no | 3.3 GiB |
| 393216 | 19.8 | 624 | 22.0 GiB | 1.67 GiB | no | 2.0 GiB |
| 524288 | 19.4 | 578 | 23.4 GiB | 33 MiB | yes | 0.5 GiB |

The selectable Arc profile is ctx 131072, q8_0 KV, plus
`mmproj-Muse-Glimmer-30B-Q8_0.gguf` (2,051,685,088 bytes, 1.91 GiB). That row
has 4.7 GiB free, so the projector fits in VRAM with about 2.8 GiB left.
ctx 393216 only has 2.0 GiB free and does not hold it. 524288 is the
Qwen-style ceiling (fit warning, half a gigabyte left). A 110k-token
fill at ctx 131072 ran at 250 t/s prefill with resident memory unchanged.
DFlash fits only at `--spec-draft-n-max 4` (14.8 t/s, 79/186 drafted tokens
accepted, 22.3 GiB) and runs out of device memory at n_max 8, including at
ctx 8192. It is not shipped. UD-Q4_K_XL stays the 16 GB default.
UD-Q5_K_XL supersedes this point on the B60, at the trained context, below.

## 2026-09-26 — Arc Pro B60 standard: Muse Glimmer 30B UD-Q5_K_XL

`Muse-Glimmer-30B-UD-Q5_K_XL.gguf`, 21,789,618,976 bytes (20.29 GiB). Same SYCL
build and flags as UD-Q5_K_M: llama.cpp 2145525, `-ngl 99 -fa on -t 6 -b 4096 -ub 2048`,
q8_0 KV, Glimmer sampler (`top-k 64`, `reasoning_strength` xhigh). Context is
the trained window, 131072. The server does not log a training-context overflow.

Server, greedy, the same short prompts used for UD-Q5_K_M:

| | TG | PP (814 tok) | VRAM | GTT | fit warning | VRAM left |
|---|---:|---:|---:|---:|---|---:|
| ctx 131072 | 16.4 | 548 | 21.6 GiB | 682 MiB | no | 2.3 GiB |

UD-Q5_K_M at this same context was 19.8 t/s in 19.2 GiB. The XL file is the
heavier quant, so decode is slower, and the extra weight uses the headroom
that previously held ctx 393216. **Shipped for the B60: UD-Q5_K_XL, ctx 131072,
q8_0 KV.** UD-Q4_K_XL stays the 16 GB default.

## 2026-09-27 — Krea 2 Turbo on Arc Pro B60: fix OpenCL discovery

Arc Pro B60 24 GB, Ryzen 5 5600, 30 GiB RAM; torch 2.14.0+xpu,
ComfyUI `79be670e`, NEO 26.35. Frozen FP8 workflow from PR #242:
1024×1024, eight Euler/simple steps, CFG 1, seed 1. Prompt:
"A blue enamel cup on a wooden table beside a window, natural afternoon
light, detailed photograph."

The long CPU-only period was VAE attention: a BF16 matrix multiply of
`[16384, 16384] @ [16384, 384]` fell back to CPU and took **105.34 s**.
oneDNN logged `Arc Pro B60 Graphics OpenCL device not found`. The NEO GPU
OpenCL library was unpacked, but the ICD discovery configuration exposed
only the CPU runtime. Setting `OCL_ICD_FILENAMES` to the unpacked
`intel-opencl/libigdrcl.so` made previously failing native GEMM and SDPA
probes succeed. `OCL_ICD_VENDORS` pointing to the library did not work with
the loader in this torch environment.

The launcher now exposes that driver. `comfyui-arc-native.patch` tries full
linear operations and native SDPA before the existing fallbacks, and keys
linear fallback decisions by row count, device and dtype as well as weight
dimensions. It applies after the original Arc patch, including on boxes
where that patch is already installed. Run `sudo bash scripts/picture.sh install`
to update an existing picture runtime.

| Stage | Original | Fixed |
|---|---:|---:|
| Text encoder load | 3.10 s | 3.08 s |
| Text encoding | 2.97 s | 2.03 s |
| KSampler (includes model loading) | 66.43 s | 28.13 s |
| VAE decode | 106.54 s | 1.65 s |
| ComfyUI prompt total | **179.42 s** | **35.26 s** |
| Picture service, including chat restoration | 214.64 s | 72.72 s |

These are single cold-process runs through the normal picture service,
which also drops file caches. GPU kernel caches were not cleared. Node
timings synchronize XPU at boundaries; the temporary profiler intercepts
linear and matmul calls and recorded no CPU calls for either operation in
the fixed run. The images were visually checked: the same composition and
detail were preserved, with small numerical/rendering differences.

The baseline hardware regression check failed because a 256-row linear
operation was split into two calls. After the patch it passes full linear,
short-prompt CPU fallback isolation, native attention and attention fallback
checks. The existing picture tests also pass (11 tests).

For profiling, temporarily copy `scripts/comfy-picture-profile.py` into
`/home/homebrain/ComfyUI/custom_nodes/`, run a picture, then remove the file.
`HB_PICTURE_PROFILE` JSON records appear in
`/home/homebrain/picture-comfy.log`; CPU seconds count CPU kernels only,
while node seconds include transfers. The hardware regression script is
`scripts/tests/check_comfy_arc.py`: run it with the ComfyUI venv Python,
the environment exported by `comfy-xpu.sh`, and `comfy/ops.py` as its argument.

## 2026-09-30 — Arc Pro B60: the first job after idle crashed the GuC

Arc Pro B60 24 GB, kernel 7.0, GuC 70.58.0, compute-runtime 26.35, oneAPI
2025.3, Muse Glimmer 30B UD-Q5_K_M with the Q8 projector, ctx 131072,
`-b 4096 -ub 2048`. Every failure had the same kernel sequence: `GuC Crash
dump notification`, a reset that fails `firmware signature verification`,
then `Xe has declared device ... as wedged`. llama-server aborts in NEO's
`checkResetStatus` (`drm_neo.cpp:290`) because the device is already gone;
that abort is a consequence. `homebrain-gpu-recover` rebound the Arc every
time (about two minutes each), without a reboot.

Test: prime a 30K-token conversation, leave the GPU idle for 300 s, then
continue the same conversation. No conversation switch and no prompt-cache
save take place, so `--cache-ram 0` is not the fix; the idle gap is.

| build | KV | Level Zero copies | runs | crashed |
|---|---|---|---:|---:|
| 2145525 (b11201) | f16 | copy engine (default) | 3 | 3 |
| 2145525 (b11201) | q8_0 | copy engine (default) | 2 | 0 |
| 2145525 (b11201) | f16 | compute queue | 2 | 0 |
| 90c908d (b11292) | f16 | copy engine (default) | 1 | 1 |
| 90c908d (b11292) | f16 | compute queue | 3 | 0 |

q8_0 KV was not immune: the 2026-09-28 and 09-29 incidents were on q8_0,
after 12 minutes and an hour of idle. f16 leaves 1.3 GiB free instead of 2.2
and turned a rare failure into a reliable one. Routing copies to the compute
queue (`UR_L0_USE_COPY_ENGINE=0`, `UR_L0_V2_FORCE_DISABLE_COPY_OFFLOAD=1`)
removed it and costs nothing measurable. The live box then served three
hourly heartbeats (36 to 59 minutes idle, each a conversation switch with a
32K-token context) on b11292 with the workaround, without a wedge.

| build | copies | chat decode (400 tok) | PP at 17K | TG at 17K |
|---|---|---:|---:|---:|
| b11201 | copy engine | 20.1 | 975 | 18.5 |
| b11201 | compute queue | 20.2 | 977 | 18.7 |
| b11292 | copy engine | 20.0 | 976 | 18.5 |

Both variables are in the SYCL runtime env in `config/versions.json`, so
`refresh_llama_runtime` puts them in the launcher and drop-in on the next
update. The SYCL build moves to b11292 (`90c908d`) with them: same speed,
and only two SYCL commits since 2145525 (multi-GPU allreduce, FWHT), neither
about this. The idea came from a report of an A770 blitter hang on the same
compute-runtime and xe KMD, where copies were likewise moved off the copy
engine; the failure here is a GuC crash rather than a `bcs` engine reset.

## 2026-11-14 — Arc Pro B60: Nemotron 3.5 Bolt 74B-A9B and flint on b12359

Arc Pro B60 24 GB, Ryzen 5 5600, 30 GiB RAM; llama.cpp b12359 (v1.0.0,
`b1b770d9`) SYCL FP16, Level Zero 26.35, oneAPI 2025.3, Level Zero copies on
the compute queue. Every row is ctx 131072 with q8_0 KV, started by the
manager's model switch so the generated unit is the catalog's. Short: an
800-token prompt and 256 decoded tokens, run twice. Deep: one long prompt and
256 decoded tokens. Free memory is `visible_avail` from the xe `vram_mm`
debugfs file.

| | file | layout | load | free idle | TG short | PP short | deep prompt | PP deep | TG deep | free after deep |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Bolt 24GB | 20.51 GiB | all on GPU, `-ub 512` | 45 s | 2.9 GiB | 17.6 | 326 | 58,620 | 390 | 16.8 | 2.6 GiB |
| flint UD-Q4_K_XL | 23.33 GiB | experts of blocks 41-48 on CPU | 20 s | 1.17 GiB | 54-58 | 780-890 | 53,398 | 468 | 39.5 | 0.97 GiB |
| flint UD-Q3_K_XL | 17.22 GiB | all on GPU | 20 s | 4.1 GiB | 60.1-60.4 | 1,086-1,181 | 53,398 | 483 | 40.5 | 3.9 GiB |

Bolt is a Mamba-2/attention hybrid with only 8 of 88 layers attending, so its
KV is ~0.53 GiB at 131072 and decode barely drops with depth; a 118,545-token
fill still ran at 372 t/s prefill and 16.1 t/s decode. flint UD-Q4_K_XL is
1.86 GiB of shared weights plus 21.47 GiB of routed experts, so 3.24 GiB of
experts go to the CPU. That costs only ~4 t/s against the all-GPU UD-Q3_K_XL,
so the higher quant is the better trade.

All three return parsed `tool_calls` from `/v1/chat/completions` (Bolt:
Qwen3-Coder XML; flint: GLM-4.5 `<arg_key>`/`<arg_value>`) and finish the
two-step round trip. Each answered an `openclaw agent` exec turn (run
`df -h /` and `free -g`, report free disk and RAM) correctly from a ~46K-token
context: Bolt 149 s, flint UD-Q4_K_XL 97 s, UD-Q3_K_XL 85 s. flint's visible
reply carried a self-correction ("1.5 exabytes... no, 1.5 terabytes").

Both need b12359. b11292 has no flint arch and refuses Bolt with
`check_tensor_dims: tensor 'blk.1.ffn_gate_inp.weight' has wrong shape;
expected 4096, 496, got 4096, 240`: Bolt varies the expert count per layer,
which arrived in llama.cpp `cd0a5b6a1` (#30520). A file that cannot load
crash-loops llama-server into its start limit.

### Bolt vision: projector on the CPU

NVIDIA ships one projector, `mmproj-NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB.gguf`
(1,733,692,736 bytes, C-RADIO v4-H, `nemotron_omni`). It encodes each image at
its own resolution, between `image_min_pixels` 262,144 and `image_max_pixels`
3,407,872 (16 px patches, 2x2 merge, so up to ~3,300 tokens). The
`nemotron_omni` loader reads those pixel limits and never calls
`set_limit_image_tokens`, so `--image-max-tokens 2048` changes nothing (the
4000x3000 image was 3,282 tokens with and without it).

Test images are synthetic: a printed invoice number plus a red circle, blue
square and green triangle. Every run read the number and placed all three
shapes correctly.

| projector | free idle | 1024x1024 (1,072 tok) | 2048x1536 (3,120 tok) | 4000x3000 (3,282 tok) |
|---|---:|---|---|---|
| GPU (default) | 1.2 GiB | 42 s, 645 MiB free | 40 s, 77 MiB free | 39 s, 31 MiB free + ~380 MiB in GTT |
| CPU (`--no-mmproj-offload`) | 2.9 GiB | 49 s, 2.6 GiB free | 136 s | 140 s, 2.6 GiB free |

Times are the whole request, including 150-460 generated tokens at 17.6 t/s.
Text decode is the same either way (17.6 t/s, also right after the largest
image). On the GPU the encoder's compute buffer grows with each larger image
until the card is full and the rest spills to GTT. **Shipped: projector on
the CPU**, which keeps the text model's 2.6 GiB margin and costs ~7 s on a
1-megapixel photo and ~100 s on the largest. With the projector in the Arc
profile, `patch_openclaw_config` lists Bolt with image input. An OpenClaw
image-tool turn read the test picture correctly in 199 s from a 50K-token
context. With the projector on the CPU, the first image after nine idle
minutes answered in 45 s, with no GuC reset, and text decode stayed at 17.6 t/s.

## 2026-11-20 — Arc Pro B60: the idle crash came back, and the card idled at 33 W

Same card and model as 2026-09-30, kernel 7.0.0-43, GuC 70.58.0,
compute-runtime 26.35, llama.cpp b12359, copies on the compute queue. The box
was off from 2026-09-30 to 11-13, so the copy-queue workaround above had a few
hours of testing, not days. From 11-14 to 11-20 the GuC crashed 8 times, the
same sequence as before. 4 resets succeeded and the reply went on; 4 failed
`firmware signature verification`, wedged the card, and cost about 2.5 minutes
of chat each.

Every crash was the first llama-server job after the GPU had been idle, almost
always the hourly heartbeat. Idle time before each of 1,012 jobs (11-13 to
11-20) against the crashes on that job:

| idle before the job | jobs | crashes |
|---|---:|---:|
| under 1 min | 877 | 0 |
| 1 to 5 min | 41 | 0 |
| 5 to 20 min | 26 | 1 |
| 20 to 45 min | 13 | 1 |
| over 45 min | 55 | 6 |

Three changes, each in its own place:

- **Keep-alive** (`gpu_keepalive.py`, `homebrain-gpu-keepalive.timer`): every
  three minutes, fill and copy 64 MiB eight times on the compute engine
  through llama-server's own Level Zero driver (`sycl-env.sh`). The GPU then
  never idles past the range where no job ever crashed. It does not go
  through llama-server: a different prompt in its only slot pushes the agent's
  conversation into the RAM prompt cache, and when that entry is evicted the
  next reply re-reads 40K to 68K tokens (40 to 80 s, seen several times on
  11-20). One run takes about 1.1 s of wall time and 96 MB of RAM; the driver
  counted about 54K compute-engine cycles for it.
- **Wedge fast path** (`99-homebrain-gpu-wedged.rules`, `recover_gpu.sh
  --wedged`): xe sends a `WEDGED` uevent with `device wedged, needs recovery`.
  The rebind used to wait until llama-server had failed five starts and the
  next timer run; now it starts on the event.
- **ASPM L1** (`gpu_aspm.sh`, `homebrain-gpu-aspm.service`): see below.

Whether the keep-alive stops the crash is not proven yet: it changes what the
GPU sees, not what llama-server's own queue sees. With no keep-alive, about
one hourly heartbeat in nine crashed. Count `GuC Crash dump` lines per day of
uptime to tell (`journalctl _TRANSPORT=kernel`, not `journalctl -k`, which
only reads the current boot).

### Idle power: ASPM

With the GPU fully idle (`gt-c6`, no display connected) the card drew 33 W
and sat at 70 °C, the memory at 78 °C, and the blower at 2,070 rpm. Every link
from the root port to the GPU had ASPM disabled: the B550 BIOS leaves it off
and Linux keeps the BIOS setting under the `default` policy. The xe package
residency counters (`/sys/kernel/debug/dri/0/dgfx_pkg_residencies`) showed
the package in G2 all the time. G8 only ever counted while the link was in
L1. Intel: ASPM L1 is required for every package state deeper than G2.

| | card power | package | chip | memory | fan |
|---|---:|---|---:|---:|---:|
| ASPM off | 33 W | G2 | 70 °C | 78 °C | 2,070 rpm |
| L1 on the Arc's links, after 20 s | 7 W | G8 | 70 °C | 78 °C | 1,527 rpm |
| the same, 2 minutes later | | G8 | 45 °C | 46 °C | 541 rpm |

The kernel only enables ASPM under a policy other than `default`, and the
policy covers every link. `gpu_aspm.sh` therefore locks every control that is
off on any other link before it switches to `powersave`, and gives the Arc's
links L1 only: no L0s, no L1 substates and no clock power management, which
need CLKREQ# that a riser may not carry. NVMe, the Realtek NIC and the chipset
keep what the BIOS gave them. No AER errors followed, and a 200-token
generation ran at 21.4 tokens/s.

### Driver updates the same evening

After #289 and #290 on .69: kernel 7.0.0-43 → 7.0.0-44, GuC 70.58.0 → 70.83.0
(`/lib/firmware/updates`, from `versions.json` `arc_firmware`), Ubuntu's
pending point releases including systemd 259.9, and Docker 29.10.0 with
Compose 5.8.0. fwupd reports the B60's own firmware (GSC, OptionROM) as
current. Prompt of 17,290 tokens, no prompt cache; the two 7.0.0-44 rows ran
without `OverrideCsrAllocationSize` (below):

| stack | prompt | generation |
|---|---:|---:|
| 7.0.0-43, GuC 70.58.0, compute-runtime 26.35 | 995 / 1,028 t/s | 19.59 t/s |
| 7.0.0-44, GuC 70.83.0, compute-runtime 26.35 | 1,029 to 1,030 t/s (4 runs) | 19.58 t/s |
| 7.0.0-44, GuC 70.83.0, compute-runtime 26.40 | 982 to 983 t/s (4 runs) | 19.50 t/s |

**Compute-runtime stays on 26.35:** 26.40.40196.8 (IGC 2.42.6) is 4.7 % slower
at prompt processing, every run. It is stable: the first 26.40 start crashed,
but that was the fault below, not 26.40.

### The keep-alive crashed llama-server: `OverrideCsrAllocationSize`

The SYCL env carried NEO's `OverrideCsrAllocationSize=1048576` (with
`NEOReadDebugKeys=1`) since the SYCL path shipped on 2026-09-26, with no
recorded reason. It shrinks each context's save area, where the GPU stores
a context's state when the GuC preempts it. With llama-server alone on the
compute engine nothing ever preempted it. The keep-alive is a second
context on that engine, and whenever it ran while llama-server was busy,
llama-server's context page-faulted (`Engine memory CAT error`, `Fault
response: Unsuccessful -ENOENT` at addresses next to each other), its engine
was reset, and llama-server aborted with `UR_RESULT_ERROR_DEVICE_LOST`.

| llama-server env | keep-alive runs overlapping llama-server's work | llama-server aborts |
|---|---:|---:|
| with the override | 3 (one model load, one prompt, one load after the restart) | 3 |
| without it | 52 (40 during a model load and two prompts, 12 during two prompts) | 0 |

Same speed without it (1,028 to 1,030 t/s prompt, 19.58 t/s generation),
and llama-server's VRAM is unchanged at 22.6 GiB of 23.9. The timer's own
runs from 18:23 to 18:52 all fell while llama-server was idle, so no agent
reply was hit, only the tests. The env no longer sets either variable,
and the keep-alive also skips a run while `/slots` reports a reply in
progress, because then the GPU is not idle anyway.

Open question: the first GuC crashes on record (2026-09-28 and 09-29) came
two days after the override arrived. Whether the two are related is not
known. The crash count per day of uptime with the override gone and the
keep-alive running will tell, but not which of the two helped.

After both reboots the boot-time ASPM unit gave the Arc's links L1 before
llama-server started, and the card idled at 8 W with the package in G8, the
fan at 37 rpm and the chip at 38 °C.

## 2026-11-21 — Arc Pro B60: Inkling-2-Mini, long context and SYCL patches

Inkling-2-Mini UD-Q4_K_XL, 26.2B MoE / 3.4B active, on the Arc Pro B60
24 GB and Ryzen 5 5600. Layers 6–17 run three times with shared weights:
24 stored layers become 48 passes per token. Use the pinned Hugging Face
`Mainline_Support` file (`inkling2_loop`), not the repository-root GGUF.
The 17,629,760,736-byte model was SHA-256 verified before these runs.

The SYCL source pin moves from b12359 (`b1b770d9`) to `19bc41dbb`, 263
upstream commits, including the loop architecture and the NVFP4 scale
fix for passes two and three. Eighteen patches are applied in order:

| patch | purpose |
|---|---|
| 0001 | Upstream PR #32740: indexed flash-attention bias and a dense-mask fallback for unsupported backends. |
| 0002 | SYCL tile-kernel indexed bias and attention sinks; unsupported kernel variants are excluded. |
| 0003 | Strided SCALE keeps SITU activation views on the GPU: graph splits fall from 194 to 2. |
| 0004 | Flatten a broadcast 2-D matrix into one GEMM; combine many tiny f32 batch products into one kernel. |
| 0005 | NVFP4 dequantization in the XMX grouped MoE GEMM. |
| 0006 | SITU as one GLU op (CPU and SYCL kernels); the graph keeps the 9-op chain where a device lacks it. |
| 0007 | SYCL fusion of `clip()`: the 8-op row-norm clip runs as one kernel. |
| 0008 | SYCL fusion of `mix()`: the 7-node depth attention runs as one kernel per token. |
| 0009 | NVFP4 decode without `pow()` or table lookups, in the MoE mat-vec and the grouped GEMM. |
| 0010 | TANH in the fused unary + mul kernel, for the two `control()` gates. |
| 0011 | Decode-size f32 mat-vec products take the small-matmul kernel instead of the generic path. |
| 0012 | SYCL fusion of a short conv with its residual ADD and SCALE (`convolve()`). |
| 0013 | Routing bias and an additive norm guard in the fused top-k MoE router. |
| 0014 | NVFP4 in the ESIMD grouped MoE GEMM, which decodes into the XMX operand without SLM staging. |
| 0015 | Indexed bias and sinks in the MKL flash-attention path (XMX GEMMs), so prefill attention leaves the tile kernel. |
| 0016 | One pass over the scores in the MKL path's softmax, and all KV heads in one pass of batched GEMMs when their scores fit. |
| 0017 | A tiled (SLM) copy for `ggml_cont` of transposed f32 matrices, such as the permuted relative-bias table. |
| 0018 | The indexed bias in the DPAS flash-attention decode kernel, which reads q8_0 K/V directly and keeps the GQA heads together. |

Upstream tracking: [ggml-org/llama.cpp#32757](https://github.com/ggml-org/llama.cpp/issues/32757).
The recipe records the commit plus `+p` and the first 12 hex digits of the
SHA-256 of the concatenated patches. Changing a patch therefore triggers a
rebuild. Applying this series to the pin reproduces tree
`48fd9a50b79f7acfd8fa64b6225a781204107335`. The diagnostic op profiler and
the `test-backend-ops` cases for 0006–0015 are excluded from the patches.

### Incremental speed measurements

Level Zero 26.35, oneAPI 2025.3, flash attention on, q8_0 KV, ctx 131072,
`-b 2048 -ub 512`, six CPU threads unless noted. The short prompt has 847
tokens; the larger prefill probe targets 6K tokens. Probes use temperature
zero and minimal reasoning for repeatability; the catalog ships at low
(0.2) since 2026-11-24.

| build | prefill, ~6K | prefill, short | decode | note |
|---|---:|---:|---:|---|
| 0001 + 0002 | 156 t/s | — | 17.1 t/s | 194 graph splits |
| + 0003 | 192 t/s | 171 t/s | 20.9 t/s | 2 splits |
| + 0004 | 258 t/s | 223 t/s | 20.9 t/s | |
| + 0005 | 391 t/s | 391 t/s | 20.9 t/s | 19.6 GiB VRAM |
| + 0005, ub2048, MMID_SCHED=3 | 422 t/s | 433 t/s | 20.9 t/s | 21.5 GiB VRAM |
| + 0006–0008 | 428 t/s | 454 t/s | 22.0 t/s | 19.6 GiB VRAM |
| + 0009–0010 | 472 t/s | 514 t/s | 32.0 t/s | 19.6 GiB VRAM |
| + 0011–0013 | 475 t/s | 517 t/s | 37.2 t/s | VRAM unchanged at 262144 |
| + 0014–0015 | 648 t/s | 655 t/s | 37.0 t/s | ctx 196608; 476 / 518 / 37.1 t/s for 0001–0013 in the same window |
| + 0016 | 820 t/s | 709 t/s | 37.0 t/s | ctx 196608; 647 / 652 / 37.0 t/s for 0001–0015 in the same window |
| + 0017 | 852 t/s | 733 t/s | — | ctx 196608; 820 / 707 t/s for 0001–0016 in the same window; short-prompt decode unchanged at 33.6 t/s |
| + 0018 | 852 t/s | 736 t/s | 37.7 t/s | ctx 196608; 852 / 735 / 37.1 t/s for 0001–0017 in the same window; decode after a 59K-token prompt 15.2 → 27.3 t/s |

The 0006–0008 row comes from window 9 on 2026-11-22. Each build served one
warm-up request before the probes, and the window ran two rounds per build.
The 0001–0005 build was slower that day than in its own row above, so only
the same-window figures compare:

| build, same window | decode, greedy | prefill, short | decode, short | prefill, ~6K |
|---|---:|---:|---:|---:|
| 0001–0005 | 19.92 / 19.91 t/s | 407.6 / 406.1 t/s | 19.02 / 19.03 t/s | 389.5 / 389.0 t/s |
| 0001–0008 | 22.02 / 22.00 t/s | 453.2 / 454.4 t/s | 20.75 / 20.84 t/s | 428.4 / 428.3 t/s |
| change | +10.5% | +11.6% | +9.3% | +10.1% |

The first 600 characters of the greedy reasoning matched, and the answer's
wording diverged after its first sentence, as expected from the changed
rounding; both answers are correct. In window 8 the fused build found both needles in the
53,666-token fill (254.0 t/s prefill, 11.89 t/s decode, against 240.2 and
11.57 for 0005 in window 7) and returned a parsed tool call.

The 0009–0010 row comes from windows 12 and 13 on 2026-11-22, run the same
way. Window 12 compared 0001–0008 with 0009's mat-vec change alone; window 13
compared that build with the full 0001–0010:

| build, same window | decode, greedy | prefill, short | decode, short | prefill, ~6K |
|---|---:|---:|---:|---:|
| window 12: 0001–0008 | 22.22 / 22.22 t/s | 453.9 / 453.1 t/s | 20.91 / 20.91 t/s | 428.1 / 428.5 t/s |
| window 12: + 0009 mat-vec | 30.61 / 30.83 t/s | 463.5 / 453.7 t/s | 30.14 / 30.31 t/s | 428.6 / 427.9 t/s |
| window 13: + 0009 mat-vec | 30.52 / 30.94 t/s | 454.8 / 454.7 t/s | 30.27 / 30.53 t/s | 428.5 / 428.9 t/s |
| window 13: 0001–0010 | 31.87 / 32.04 t/s | 514.2 / 513.2 t/s | 30.92 / 30.85 t/s | 471.8 / 471.9 t/s |

From 0001–0008 to 0001–0010, decode rises 44% and prefill 10–13%. The
printed start of the greedy reasoning and answer (300–400 characters) matched
across all three builds. In window 12 the mat-vec build found both needles in
a 59,054-token fill at 264.7 t/s prefill and 14.61 t/s decode, and returned a
parsed tool call.

`-ub 512` keeps the memory margin for long context. The extra ~8% prefill
at ub2048 costs ~1.9 GiB without improving decode.

f16 KV does not speed up prefill either. Window 7 compared it with q8_0 at
ctx 131072 and ub512:

| KV | prefill, short | prefill, ~6K | 53,666-token fill | decode after the fill | VRAM after the fill |
|---|---:|---:|---:|---:|---:|
| f16 | 370.8 t/s | 384.7 t/s | 240.7 t/s | 13.00 t/s | 21.07 GiB |
| q8_0 | 381–391 t/s | 388.6 t/s | 240.2 t/s | 11.57 t/s | 19.60 GiB |

f16 gains 12% decode at depth and costs 1.5 GiB at 131072. At 262144 it would
not fit, so q8_0 stays the long-context choice. Both found the two needles.

### Long context and vision

Window 4 used ctx 262144, q8_0 KV and ub512 with the F16 projector:

| probe | prefill | decode | VRAM |
|---|---:|---:|---:|
| load, 17 s | — | — | 23.24 GiB |
| 847-token short prompt | 391.1 t/s | 19.90 t/s | |
| 58,723-token two-needle fill | 233.6 t/s | 11.46 t/s | 23.31 GiB |
| short prompt after the fill | 407.2 t/s | 19.85 t/s | |

Both needles were retrieved correctly: code 4417 and captain Odile Varga.
The image probe correctly described red and blue halves with a central
white square (85 prompt tokens at 27.3 t/s). Vision works at 262K, but its
less-than-1-GiB headroom does not meet the profile's memory-margin target.
The selected **text-only 262144** profile was measured separately:
22.55 GiB after loading and 22.60 GiB after the 58,723-token fill, leaving
about 1.3 GiB on the 23.9 GiB device. The short prompt ran at 380.2 t/s
prefill and 19.78 t/s decode. The deep fill retrieved both needles at
248.6 t/s prefill and 11.56 t/s decode (245 s wall time). These tests verify
allocation at 262K and retrieval through 58.7K; they do not test a full
262K-token prompt.

With 0006–0008 (window 10) the text-only 262144 profile used the same memory:
22.55 GiB after loading and 22.60 GiB after the fill. The fill retrieved both
needles at 263.6 t/s prefill and 11.91 t/s decode (232 s wall time). The short
prompt ran at 452.2 t/s prefill and 20.79 t/s decode, and 457.1 t/s and
20.78 t/s after the fill. Vision was not re-measured on 0006–0008.

With 0009–0010 (window 14) memory is unchanged: 22.55 GiB after loading and
22.60 GiB after the fill. The fill retrieved both needles at 278.6 t/s prefill
and 14.66 t/s decode (218 s wall time). The short prompt ran at 515.7 t/s
prefill and 30.96 t/s decode, and 517.5 t/s and 30.93 t/s after the fill.
Greedy decode ran at 31.67 t/s, and the tool call parsed.

With 0011–0013 (window 17) memory is again unchanged: 22.55 GiB after loading
and 22.60 GiB after the fill. The fill retrieved both needles at 279.0 t/s
prefill and 15.23 t/s decode (217 s wall time). The short prompt ran at
519.7 t/s prefill and 33.61 t/s decode, greedy decode at 37.15 t/s, and the
tool call parsed.

**Vision, windows 18 and 19 (0001–0013).** The profile now loads the F16
projector (`inkling2v`, 639,348,160 bytes) on the GPU at ctx 196608. The
encoder caps an image at 4,096 tokens and resizes larger ones, so a
4000x3000 photo (4,057 tokens) is near the worst case. The test images are
synthetic: a printed invoice number and a red circle, a blue square and a
green triangle. Every run read the number and named the shapes in order.
Times are the whole request with about 90–110 generated tokens. "Peak" is
after the largest image and a 58,723-token fill:

| configuration | after load | peak | 1024x1024 (718 tok) | 2048x1536 (2,070 tok) | 4000x3000 (4,057 tok) |
|---|---:|---:|---|---|---|
| 262144, text only | 22.55 GiB | 22.60 GiB | — | — | — |
| 262144, projector on the GPU | 23.24 GiB | 23.72 GiB | 7.4 s | 8.1 s | 14.0 s |
| 262144, projector on the CPU | 22.55 GiB | 22.62 GiB | 9.3 s | 32.3 s | 91.5 s |
| 262144, CPU, `--image-max-tokens 1024` | 22.55 GiB | 22.62 GiB | 8.8 s | 13.8 s (1,014 tok) | 13.9 s (1,014 tok) |
| **196608, projector on the GPU (shipped)** | 21.74 GiB | 22.21 GiB | 5.7 s | — | 16.2 s |

On the GPU, the encoder's buffer grows with the image and then stays: the
fill after the 4000x3000 image added nothing. At 262144 that leaves 0.2 GiB
of the 23.9 GiB card. The shipped 196608 keeps 1.7 GiB and full image
detail, more margin than the text-only 262144 profile had. Text speed does
not change with a projector (short prompt 516–518 t/s prefill, 33.7 t/s
decode, also right after an image). The 58,723-token fill probe, which runs
with prompt caching on, was slower with a projector: 261.7 / 15.01 t/s on the
GPU and on the CPU alike, against 279.0–279.2 / 15.23 t/s text-only in windows
17 and 19. Both found both needles. Window 20 repeated the comparison in one
window, without the fill probe: a 19,644-token prompt with caching off, two
rounds each at ctx 196608, projector on the CPU. Text-only ran at 403.5 /
402.9 / 403.5 / 403.1 t/s and with the projector at 403.6 / 403.1 / 403.7 /
403.0 t/s, and the per-op profiles match. So a loaded projector does not slow
the kernels. Whether the fill gap comes from the server's handling of a cached
prompt with a projector, or from the deeper context, is not established.

Earlier, with only patches 0001–0002, 262144 text-only loaded at 22.6 GiB,
131072 with the projector at 20.3 GiB, and a 31.5K-token fill retrieved both
needles at 141 t/s. Tool calls returned parsed `tool_calls`; a cold OpenClaw
exec turn answered correctly in 398 s. These earlier results are not speed
measurements of the final patch series.

### Regression and correctness checks

Glimmer UD-Q5_K_M, production settings and the same 7.6K-token prompt:

| binary | prefill | decode | output |
|---|---:|---:|---|
| production b12359 | 956 t/s | 21.5 t/s | identical |
| scratch pin + 0001–0005 | 972 t/s | 21.8 t/s | identical |
| production v2026.11.22, pin + 0001–0005 | 962 t/s | 21.7 t/s | identical |
| scratch pin + 0001–0008, same window | 959 t/s | 21.7 t/s | identical |
| scratch pin + 0001–0009 mat-vec, window 12 | 961 t/s | 21.7 t/s | same opening |
| scratch pin + 0001–0010, window 13 | 973 t/s | 21.7 t/s | same opening |
| scratch pin + 0001–0012, window 16 | 974 t/s | 21.8 t/s | same opening |
| scratch pin + 0001–0013, window 17 | 974 t/s | 21.8 t/s | same opening |
| scratch pin + 0001–0015, window 22 | 975 / 977 t/s | 21.8 t/s | same opening |
| scratch pin + 0001–0016, window 26 | 973 / 976 t/s | 21.7 / 21.8 t/s | same opening |
| scratch pin + 0001–0017, window 27 | 972 / 973 t/s | 21.8 t/s | same opening |

In window 22, 0001–0013 ran at 973 t/s and 21.8 t/s between the two
0001–0015 rounds; in window 26, 0001–0015 ran at 975 t/s between the two 0001–0016 rounds; in window 27, 0001–0016 ran at 973 t/s between the two 0001–0017 rounds. For the last seven rows only the first 70 characters of the 200-token answer
were compared; they match production.

`test-backend-ops` against the CPU passed SCALE 7/7; MUL_MAT f32 318/318,
f16 278/278, bf16 239/239, NVFP4 42/42; and MUL_MAT_ID NVFP4 73/73 under
schedulers 1 and 3. Q8_0 MUL_MAT_ID passed 114/115: the remaining case
failed to allocate 1.1 GiB while Glimmer was resident, rather than failing
numerically. The dedicated indexed-bias kernel test passed.
On 0001–0010, NVFP4 passed MUL_MAT 42/42 and MUL_MAT_ID 73/73 again, and the
fused TANH cases passed 39/39 (unary + mul) and 10/10 (add + unary + mul).
On 0001–0012, MUL_MAT f32 passed 277/277, including scratch cases for every
branch of the new mat-vec kernel. MUL_MAT_HADAMARD passed 32/32, SSM_CONV
75/75, the existing conv + bias + SiLU fusion 120/120, and the new conv +
residual + scale fusion 42/42. On 0001–0013, TOPK_MOE passed 698/698,
including Inkling's router at 1, 5 and 512 tokens. `GGML_SYCL_DEBUG` traces
confirmed that both new fusions ran instead of their unfused ops. On
0001–0014, NVFP4 passed MUL_MAT_ID 73/73 and MUL_MAT 42/42 again. The CPU has
no indexed bias, so 0015 is checked on the model instead; see 0014–0015 below.
FLASH_ATTN_EXT passed every case it ran, with no numeric failures:

| run | result |
|---|---|
| full sweep, GPU free | 4,309 cases OK, 0 FAIL; stopped by its 900 s timeout, so incomplete |
| subset `sinks=1` | 2,102/2,102 |
| subset `type_K=f16,type_V=f16` | 1,975/1,975 |
| subset `type_K=q8_0,type_V=q8_0` (the shipped KV type) | 373/373 |

The three subsets ran next to Glimmer; unsupported cases are skipped.
Bolt, flint and Qwen have not been re-tested on this pin. The production
update/rebuild path has not been deployed; scratch execution and patch-tree
reproduction do not establish a successful installed-version migration.

### Remaining speed costs

A diagnostic profiler synchronizes after every op and charges the time to the
op and its weight name. Small-op timings therefore include extra
synchronization and are not normal throughput. The probe was two ~6K-token
prompts at the shipped ub512, 60 graphs in total; prefill dropped from
391 t/s to 362–367 t/s under the profiler:

| operation | time in a 39.6 s profile | share |
|---|---:|---:|
| indexed-bias tile flash attention (prefill) | 8.90 s | 22% |
| batched NVFP4 MUL_MAT_ID (`ffn_gate_up_exps` 7.10 s, `ffn_down_exps` 3.69 s) | 10.79 s | 27% |
| element-wise operations and copies (MUL, CONT, CONCAT, SQR, CPY, TANH, SCALE, RMS_NORM, ADD, SUB, …) | ~11 s | ~28% |
| tiny f32 MUL_MAT (depth mixing and the bias table) | ~1.5 s | 4% |
| decode-size NVFP4 MUL_MAT_ID | 1.52 s | 4% |
| `attn_sink_proj` bf16 MUL_MAT, [2304 × 24] | 1.03 s | 3% |
| dense q8_0 attention projections | ~1.0 s | 3% |

SYCL graph replay does not help decode. With `GGML_SYCL_ENABLE_GRAPH=1`,
graphs whose MUL_MAT uses oneDNN are not recorded (`disabling SYCL graphs
due to MUL_MAT using oneDNN`). With oneDNN also off
(`GGML_SYCL_ENABLE_DNN=0`):

| | graph replay | baseline |
|---|---:|---:|
| decode, 377-token greedy | 21.20 t/s | 20.89 t/s |
| decode, short prompt | 19.44 t/s | 19.79 t/s |
| prefill | 389–391 t/s | 389–391 t/s |

So host launch overhead is not what bounds decode. Inkling runs about 12,400
graph nodes per token, and the likelier bound is their GPU-side cost. Fusing the element-wise
chains (SITU, the depth mixing, clip and control) reduces that count, and is
the lever for both prefill and decode.

This held for 0001–0005 only. After 0009 made the NVFP4 mat-vec 3.7x faster,
decode became bound by host dispatch; see 0011–0013 below.

The reported 14,100 t/s on an RTX 5090 used a custom engine with native FP4
tensor-core execution. This B60 path dequantizes NVFP4 into XMX GEMM and
runs a different graph and attention implementation. That number is not a
like-for-like llama.cpp or hardware comparison, nor a B60 performance target.

### Fusion, 0006–0008

`test-backend-ops` against the CPU passed SITU 16/16 (f32 and f16; bf16 is
unsupported, as for every SYCL GLU op), the `clip()` chain 6/6 and the `mix()`
subgraph 11/11. Its 6-state case exercises the unfused fallback. The CPU SITU
kernel is bit-identical to the 9-op chain in f32.

The same profiler on the fused build, one ~6K-token prompt plus its 16 decode
steps (31 graphs, 21.1 s):

| operation | share |
|---|---:|
| indexed-bias tile flash attention | 21% |
| batched NVFP4 MUL_MAT_ID | 26% |
| decode-size NVFP4 MUL_MAT_ID | 5% |
| `attn_sink_proj` bf16 MUL_MAT, [2304 × 24], decode-size | 5.4% |
| MUL | 5.6% |
| attention-gate SIGMOID | 4.0% |
| recurrent conv-state CPY | 3.7% |
| bias-table permute CONT | 2.4% |
| CONCAT | 2.2% |
| fused SITU | 1.0% |

The SITU, `clip()` and `mix()` chains are gone from the profile. This
profile started from a fresh server, so first calls include SYCL's JIT
compilation, and the `attn_sink_proj` and SIGMOID rows are inflated by it.
Over 400 decode tokens `attn_sink_proj` averages 75 µs per call under the
profiler, against 9.2 µs in isolation. The SIGMOID row is the fused attention
gate.

### NVFP4 decode and TANH, 0009–0010

A 400-token decode profile of 0001–0008 ranked the decode-size NVFP4
MUL_MAT_ID first, at 355 µs per `ffn_gate_up_exps` call and 198 µs per
`ffn_down_exps` call under the profiler. `test-backend-ops` perf measured the
kernel alone at 128 experts, top-8 and Inkling's shapes. Over 44 NVFP4 layers
its 1-token times add up to about 21 ms of a 45 ms token:

| type | gate_up, 1 token | down, 1 token | gate_up, 512 tokens | down, 512 tokens |
|---|---:|---:|---:|---:|
| NVFP4, 0001–0008 | 300.9 µs | 182.9 µs | 7193 µs | 3637 µs |
| NVFP4, 0001–0010 | 82.3 µs | 50.3 µs | 5534 µs | 2942 µs |
| MXFP4, for reference | 73.2 µs | 43.2 µs | 6212 µs | 3706 µs |
| Q8_0, for reference | 105.9 µs | 40.4 µs | 2662 µs | 1382 µs |

MXFP4 stores the same E2M1 codes, so the 4x gap was decoding, not bandwidth.
Each 16-value scale went through a `sycl::pow()` call, and the codes went
through emulated byte permutes. 0009 builds the scale from bits and decodes
four codes at a time. The 512-token grouped GEMM is still about twice the Q8_0
time.

0010 lets the existing unary + mul fusion take TANH, which removes 192 MUL
launches per decode token from `control()`.

SYCL graph replay was re-tested on 0001–0010 with oneDNN off (window 13). It
raised the 35-token greedy decode from 31.46 / 30.91 to 34.23 / 34.24 t/s, but
the short-prompt decode only from 30.25 to 30.76 t/s. With oneDNN off the
greedy text also changes: it stops at 359 tokens instead of running to 400. It
is not enabled.

### Host-bound decode, 0011–0013

After 0009–0010 the main llama-server thread ran at 99.7% CPU during decode
(window 15, `top -H`), so decode was bound by host dispatch. The profiler,
extended to record host time per op, put the generic MUL_MAT path at 15–19 µs
of host time for each f32 product of the loop control, the router and the bias
table. That is 384 calls per token. A plain kernel launch costs about 5 µs.

0011 sends a 2-D f32 product with at most 32 columns and at most 2^20 MACs to
0004's small-matmul kernel. When K ≥ 64 a work-group per output splits K across
float4 loads. A sub-group per output would leave `ctrl_down`, which has 64 rows
of 4608, latency-bound. In `test-backend-ops` perf these products take
3.6–5.7 µs per call, against 9.54 µs for the one shape the 0001–0010 test build
also has (24 × 2304). 0012 runs the 176 non-dynamic `convolve()` chains
(SSM_CONV, RESHAPE, ADD, SCALE) as one kernel, cutting ADD launches from 496 to
320 per decode token. 0013 runs Inkling's router as one kernel: sigmoid, the
selection bias, top-8, then the weight norm guarded by `sum + eps`.

Windows 16 and 17 ran on 2026-11-22, as before: ctx 131072, q8_0 KV, ub512, one
warm-up per build, two rounds:

| build, same window | decode, greedy | prefill, short | decode, short | prefill, ~6K | decode after 6K |
|---|---:|---:|---:|---:|---:|
| window 16: 0001–0010 | 31.71 / 31.78 t/s | 513.6 / 513.9 t/s | 30.70 / 30.87 t/s | 472.2 / 472.2 t/s | 26.91 / 26.92 t/s |
| window 16: 0001–0012 | 36.34 / 36.33 t/s | 517.6 / 515.1 t/s | 32.72 / 32.78 t/s | 473.3 / 473.6 t/s | 28.32 / 28.43 t/s |
| window 17: 0001–0012 | 35.79 / 36.21 t/s | 515.1 / 514.1 t/s | 32.75 / 32.81 t/s | 473.7 / 473.4 t/s | 28.40 / 27.95 t/s |
| window 17: 0001–0013 | 37.15 / 37.16 t/s | 516.9 / 517.6 t/s | 33.46 / 33.49 t/s | 475.1 / 474.5 t/s | 28.86 / 28.91 t/s |

From 0001–0010 to 0001–0013, greedy decode rises about 17%, short-prompt decode
9% and decode after the 6K prompt 7%. Prefill is unchanged. Under the profiler,
host dispatch fell from 34.3 ms per decode token (0001–0010) to 27.5 ms
(0001–0012) and 25.7 ms (0001–0013). 0011 changes the f32 summation order, so
the greedy answer differs from 0001–0010's from its first sentence, and the
reasoning diverges after about 200 characters. 0013 changes the answer again.
All three answers are correct. The printed 300-character openings of each
build's two rounds match.

The largest remaining decode node groups are the `mix()` CONCAT chains, the
conv-state CONCAT and CPY, and the NVFP4 scale REPEAT and GET_ROWS. In window
15's prefill profile of 0001–0010, flash attention took 32% and the NVFP4
grouped GEMM 29%; 0011–0013 do not change either.

Prefill still has headroom in both. Window 20 profiled a warm 19,644-token
prompt on 0001–0013. Flash attention took 45% of the time, at 11.8 ms per
call, and the NVFP4 GEMMs 27%. The indexed bias keeps Inkling's attention
off every XMX path (oneDNN SDPA, the MKL GEMM path and the DPAS kernel), so
it runs on the SIMD tile kernel. The NVFP4 grouped GEMM takes 5.1 ms per
`gate_up` call against 2.2 ms for the q8_0 layers of the same shape. q8_0
has an ESIMD grouped GEMM and NVFP4 does not. At 512 tokens per ubatch the
NVFP4 call runs at about 8 TFLOPS, far below both the card's XMX peak and
its memory bound (0.84 ms to read all 128 experts). 0014 and 0015 below take
both levers.

### Prefill, 0014–0015

0014 adds NVFP4 to the ESIMD grouped GEMM. A 32-value k step is two 16-value
sub-blocks, one per dpas k half, each with one UE4M3 scale. Every code times
a scale is exact in half. In `test-backend-ops` perf (128 experts, top-8),
with `GGML_SYCL_MMID_ESIMD` switching the kernel:

| MUL_MAT_ID | joint_matrix | ESIMD |
|---|---:|---:|
| NVFP4 `gate_up` [2304 × 2304], 512 tokens | 5,537 µs | 3,421 µs |
| NVFP4 `down` [1152 → 2304], 512 tokens | 2,938 µs | 1,704 µs |
| NVFP4 `gate_up`, 8 tokens | 1,494 µs | 983 µs |
| q8_0 `gate_up` / `down`, 512 tokens, for reference | — | 2,671 / 1,383 µs |

NVFP4 is still slower than q8_0 here: its weight stays in the canonical
block layout, read with gathers, where q8_0 is reordered for 2D block loads.

0015 lets the MKL flash-attention path take the indexed bias and sinks. That
path runs QK and PV as XMX GEMMs per KV head with an online softmax over
8,192-key chunks. The softmax adds the bias exactly as the tile kernel does.
Inkling's 24 layers are 20 sliding-window layers, each with its own
1,024-key cache, and 4 full ones. With the loop, 40 of the 48 attention
passes per token see about 1,500 keys in prefill. A scratch perf case with
512 queries, 24 heads over 8 KV heads, q8_0 K/V and 1,025 bins:

| keys | tile kernel | MKL path |
|---:|---:|---:|
| 1,536 | 5.87 ms | 5.53 ms |
| 2,048 | 7.36 ms | 5.97 ms |
| 3,072 | 12.89 ms | 7.15 ms |
| 4,096 | 15.90 ms | 8.02 ms |
| 6,144 | 23.99 ms | 10.47 ms |
| 10,240 | 38.51 ms | 12.11 ms |
| 20,480 | 74.65 ms | 20.26 ms |

Window 21 ran on 2026-11-24 at the catalog's ctx 196608, q8_0 KV, ub512, one
warm-up per build and two rounds. "+ 0014" is the 0001–0015 build with
`GGML_SYCL_ENABLE_MKL_FA=0`:

| build, same window | prefill, 847 tokens | prefill, 5,904 | prefill, 19,644 | decode, short |
|---|---:|---:|---:|---:|
| 0001–0013 | 516.9 / 518.5 t/s | 475.9 / 475.4 t/s | 403.0 / 403.0 t/s | 33.60 / 33.59 t/s |
| + 0014 | 638.3 / 638.6 t/s | 556.3 / 556.0 t/s | 457.7 / 457.8 t/s | 33.57 / 33.58 t/s |
| + 0014–0015 | 656.6 / 653.4 t/s | 649.1 / 647.2 t/s | 624.8 / 623.8 t/s | 33.48 / 33.58 t/s |

Prefill rises 26% on the short prompt, 36% at 5.9K tokens and 55% at 19.6K.
Decode is unchanged; greedy decode ran at 37.04 t/s against 37.14. Under the
profiler, a warm 19,644-token prompt took 34.3 s of op time against 51.5 s on
0001–0013. Flash attention fell from 23.3 s to 11.9 s (11.8 to 6.0 ms per
call) and the NVFP4 GEMMs from 13.7 s to 7.9 s. They still hold 35% and 23%,
then the permuted CONT at 5%. The MKL path spends about 570 µs of host time
per call. That launch work runs ahead of the GPU in prefill.

The checks:

- Attention path. On the same 0001–0015 build, the MKL path off and on were
  given one 24,558-token prompt of llama.cpp documentation, then 32 greedy
  tokens were drawn with their top-5 probabilities. The text was identical,
  with the same argmax at all 32 positions. Most top-5 probabilities moved by
  0.02 or less. The largest move, 0.12, came at a near tie ("on Android,
  Windows" against "on Android and Windows"). 0014 alone against 0001–0013
  changed the greedy text after 11 tokens, through the f32 summation order,
  with at most 0.04 before the split.
- Shipped vision profile, ctx 196608 with the GPU projector, on 0001–0015.
  The 58,723-token fill found both needles at 546.8 t/s prefill and
  15.23 t/s decode; window 19 measured 261.7 and 15.01 on 0001–0013. The
  4000x3000 test image was read correctly in 14.4 s (16.2 s before). VRAM
  was 21.74 GiB after loading, 21.83 GiB after the fill and 22.26 GiB after
  the image, against a 22.21 GiB peak on 0001–0013.
- Glimmer is unchanged (window 22, in the regression table above).

### Prefill, 0016

After 0015, the MKL path's softmax still computed every score twice: once
for the row maximum and once for the numerators, each time gathering the
bias index and the table entry. 0016 stores the final score in the first
pass and reads it in the second. The work-groups of one query in a pass's
heads run next to each other, so they share the mask, bias-index and table
lines in cache. Q packing and the final normalization take one launch per
pass instead of one per Q head. When the scores of all 8 KV heads for one
chunk fit 24M elements, the heads run as one pass of batched GEMMs. That
covers the sliding-window layers: about 1,500 keys × 1,536 query rows × 8.
`GGML_SYCL_MKL_FA_BATCH_HEADS=0` turns the batching off. The scratch perf
case from 0015, both builds in one run:

| keys | 0015 | 0016 |
|---:|---:|---:|
| 1,536 | 5.60 ms | 1.53 ms |
| 2,048 | 6.01 ms | 1.76 ms |
| 3,072 | 7.21 ms | 3.83 ms |
| 4,096 | 7.96 ms | 4.21 ms |
| 6,144 | 10.45 ms | 6.14 ms |
| 10,240 | 12.17 ms | 8.66 ms |
| 20,480 | 20.33 ms | 15.85 ms |

From 3,072 keys the scores exceed the budget and the KV heads run one at a
time. With the batching off, 1,536 keys take 2.54 ms. `test-backend-ops`
against the CPU passed 20 scratch cases of Inkling's shape without the bias
(f16 and q8_0 K/V, 1,024 to 9,000 keys, 32 and 512 queries) with the
batching on and off, and the FLASH_ATTN_EXT kv=1024 slice (232/232). The CPU
has no indexed bias, so the model probe below checks it.

Window 26 ran on 2026-11-25 with the same settings as window 21:

| build, same window | prefill, 847 tokens | prefill, 5,904 | prefill, 19,644 | decode, short |
|---|---:|---:|---:|---:|
| 0001–0015 | 651.6 / 652.1 t/s | 647.5 / 646.9 t/s | 623.5 / 623.4 t/s | 33.60 / 33.60 t/s |
| + 0016, batching off | 694.8 / 692.5 t/s | 772.1 / 771.8 t/s | 752.9 / 752.7 t/s | 33.58 / 33.58 t/s |
| + 0016 | 711.0 / 706.8 t/s | 819.9 / 820.4 t/s | 799.5 / 799.2 t/s | 33.58 / 33.59 t/s |

Prefill rises 9% on the short prompt, 27% at 5.9K tokens and 28% at 19.6K.
About a quarter of the gain is the batching. Decode is unchanged; greedy
decode ran at 37.05 t/s on both builds. Under the profiler, a warm
19,644-token prompt took 27.5 s of op time against 34.3 s for 0001–0015 in
window 21. Flash attention fell from 11.9 s to 5.0 s (6.0 to 2.6 ms per
call), and its host time per call from 571 µs to 171 µs. The NVFP4 GEMMs
now lead at 7.9 s (29%), then attention at 18% and the permuted CONT at 6%.

The checks:

- Attention path. The window-21 probe was rerun: 24,558 tokens of
  documentation, then 32 greedy tokens with their top-5 probabilities.
  0001–0016, with the batching on and off, against 0001–0015: identical
  text and the same argmax at all 32 positions. The largest top-5 move was
  0.13, while 0001–0015 against its own window-21 run moved up to 0.16.
- Shipped vision profile, ctx 196608 with the GPU projector, on 0001–0016.
  The 58,723-token fill found both needles at 684.8 t/s prefill (546.8 on
  0001–0015) and 15.24 t/s decode. The 4000x3000 test image was read
  correctly in 13.1 s (14.4 s). VRAM was 21.74 GiB after loading, 21.92 GiB
  after the fill and 22.34 GiB after the image. That is 0.09 GiB above
  0001–0015, from the up to 167 MB of scores a batched pass holds.
- Glimmer is unchanged (window 26, in the regression table above).

### Prefill, 0017

After 0016, the permuted CONT of the relative-bias table was 6% of a warm
19.6K-token prompt. The table is [1,025 bins × 24 heads] per token,
permuted to heads-first once per layer pass. SYCL copied it element by
element, so every read strode by a row. 0017 ports CUDA's tiled transposed
copy: each 32 × 32 tile goes through SLM, so reads and writes coalesce. It
applies to any `ggml_cont` of transposed f32 matrices. In
`test-backend-ops` perf:

| copy | element-wise | tiled |
|---|---:|---:|
| bias table [1025, 24, 512], permute 1,0,2,3 | 893 µs | 363 µs |
| [2304, 5, 512] transposed (`mix()` states) | 394 µs | 366 µs |
| [768, 1024, 64] transposed | 3,676 µs | 1,068 µs |

CPY passed 397/397 against the CPU and CONT 91/91, including five scratch
cases of these shapes and tile edges. The f8_e4m3 copies were excluded:
SYCL has no non-contiguous f8_e4m3 copy, and they abort before 0017 too.

Window 27 ran on 2026-11-25 with the settings of window 21:

| build, same window | prefill, 847 tokens | prefill, 5,904 | prefill, 19,644 | decode, short |
|---|---:|---:|---:|---:|
| 0001–0016 | 705.8 / 707.5 t/s | 819.1 / 819.8 t/s | 799.1 / 798.6 t/s | 33.53 / 33.47 t/s |
| + 0017 | 733.5 / 732.9 t/s | 852.0 / 851.6 t/s | 829.3 / 829.0 t/s | 33.61 / 33.62 t/s |

Prefill rises 3.7–3.9% across the three lengths. In the server the CONT
fell from 571 to 256 µs per call, and the warm 19,644-token prompt from
27.5 s to 26.6 s of op time. The copy does no arithmetic, so outputs are
unchanged. Glimmer is unchanged (window 27, in the regression table above).

### Decode, 0018

0001 hands Inkling's attention a compact bias: a per-query table of 1,025
bins and a bin index per key. Until 0018 only the tile kernel read it, so
every decode step took that kernel. The tile kernel dequantizes q8_0 K/V
through a scalar loop and runs each of the GQA heads in its own
work-group, so the cost of a decode step grew with the KV length. SYCL
already had a faster decode kernel for quantized caches: DPAS (s8 XMX)
products for Q·K and P·V, all heads of a KV group in one work-group, and the
keys split across the device with a combine pass. 0018 adds the bias to
that kernel. Each score's mask value gets `table[bin]`, rounded to F16 as
the dense mask it replaces is; a bin outside the table adds nothing. Up to
31 query rows take the DPAS kernel, larger batches keep the tile or MKL
path.

`test-flash-attn-indexed-bias` compares against a double-precision host
reference. For q8_0 and q4_0 K/V, 1, 2, 7 and 31 queries, and 16, 768 and
5,000 cells, the largest error was 6.0e-5. With the DPAS kernel switched
off (`GGML_SYCL_FA_DPAS=0`) the errors differ, from the tile kernel, so the
DPAS path is what ran.

Window 29 ran on 2026-11-26 with the settings of window 21, text-only:

| build, same window | decode, short | decode, greedy | decode after 5,904 | after 19,644 | after 59,042 |
|---|---:|---:|---:|---:|---:|
| 0001–0017 | 33.62 / 33.60 t/s | 37.09 t/s | 28.91 t/s | 23.47 t/s | 15.22 t/s |
| + 0018 | 37.32 / 37.34 t/s | 37.66 t/s | 34.56 t/s | 32.42 t/s | 27.32 t/s |
| 0001–0017 again | 33.62 / 33.60 t/s | 37.09 t/s | | | |

The short prompt has 847 tokens and the greedy one 35. The "after" columns
are 16-token answers to the prefill probes and the two-needle fill; their
decode now loses 27% from 35 to 59K tokens of context, against 59% before.
Prefill is unchanged: 732.0 / 739.4 t/s short, 851.8 t/s at 5,904 tokens,
828.7 at 19,644 and 708.1 at 59,042 (731.1 / 738.6, 852.1, 829.5 and 708.0
on 0001–0017). Both needles were retrieved on both builds.

The top-5 logprob probe on a 24.5K-token prompt agreed on the first 11 of
32 tokens and split at the twelfth, where 0001–0017 ranks " Open" over " the" at 0.304 / 0.301 and 0018 ranks
" the" first at 0.341 / 0.297: a near tie that the changed accumulation
order flips. Before the split the largest top-5 probability difference was
0.027. The DPAS split-K buffers come from the pool and are at most a few MB
at decode sizes. Glimmer and the other models do not use the indexed bias,
so their attention takes the same kernels as before.


## 2026-11-27 — Arc Pro B60: Inkling on the llama.cpp master pin

The SYCL source pin moves from `19bc41dbb` to master `98cc730cc`
(2026-11-26), 176 upstream commits. Upstream now does part of what the
series did:

- #32740 puts the indexed bias on `FLASH_ATTN_EXT`, and #33053 uses it in
  the Inkling graph. Master adds the bias in F32 from a contiguous
  [bins, heads, queries] table; a backend that reports it unsupported gets
  a dense mask instead. 0001 is dropped, and 0002, 0015 and 0018 are
  re-targeted to that table.
- #32940, #32855 and #32948 rebuild `clip()`, `mix()` and `control()` with
  fewer ops, and #33060 fuses the new chains in CUDA. 0006 (the SITU op),
  0007 (clip), 0008 (depth mix) and 0012 (conv residual) matched graph
  shapes that no longer exist. The new 0019 ports #33060 to SYCL instead.
- 0005, 0009 and 0014 become one patch, 0005, the commit of the open
  upstream PR #32981.

Eighteen patches are applied in order. Patch numbers are kept, so a number
means the same change in both series:

| patch | purpose |
|---|---|
| 0002 | SYCL tile-kernel indexed bias and attention sinks; unsupported kernel variants are excluded. |
| 0003 | Strided SCALE keeps SITU activation views on the GPU. |
| 0004 | Flatten a broadcast 2-D matrix into one GEMM; combine many tiny f32 batch products into one kernel. |
| 0005 | NVFP4 MoE: decoding without `pow()` or tables, in the mat-vec, the XMX grouped GEMM and the ESIMD grouped GEMM. |
| 0010 | TANH in the fused unary + mul kernel. |
| 0011 | Decode-size f32 mat-vec products take the small-matmul kernel instead of the generic path. |
| 0013 | Routing bias and an additive norm guard in the fused top-k MoE router. |
| 0015 | Indexed bias and sinks in the MKL flash-attention path (XMX GEMMs). |
| 0016 | One pass over the scores in the MKL path's softmax, and all KV heads in one pass of batched GEMMs when their scores fit. |
| 0017 | A tiled (SLM) copy for `ggml_cont` of transposed f32 matrices. |
| 0018 | The indexed bias in the DPAS flash-attention decode kernel. |
| 0019 | SYCL port of #33060: situ, the L2 clip, the short conv with its state store, the depth mix and the loop controller as fused kernels. |
| 0020 | NVFP4 expert weights reordered into [qs][d] for the MoE mat-vec and the grouped GEMMs. |
| 0021 | f16 and bf16 decode mat-vecs take 0011's kernel: Inkling's bf16 attention-sink projection. |
| 0022 | 1024-item work-groups for the decode-size depth mix. |
| 0023 | SYCL port of the CUDA MoE weighted expert reduction (#25952): the scale, router-weight and sum chain after the down projection in one kernel. |
| 0024 | The per-expert weight scale of NVFP4's MUL_MAT_ID (RESHAPE, REPEAT, GET_ROWS, MUL) in one kernel. |
| 0025 | The DPAS decode attention kernel overlaps its K/V loads with compute, and P·V reads each V block once. |

Applied to `98cc730cc`, the series reproduces the llama.cpp branch
(`4e6a8b1de`) outside `tests/` and `docs/`, and the build id is
`98cc730cc8f8f798cf83d22c23a072c205b0e510+pec1dd53b7cd6`. The
`test-backend-ops` cases for the ported fusions, the NVFP4 MoE shapes, the
Inkling decode shapes and the decode-attention sweep live on that branch
and are excluded from the patches.

### Speed

Against 0001–0018 (#306), the series decodes 27% faster at a short context
(37.3 → 47.3 t/s), 29% after 5,904 and 19,644 tokens (34.5 → 44.6 and
32.4 → 41.9 t/s) and 32% after 59K (27.3 → 36.1 t/s). It prefills 12–14%
faster up to 20K tokens (852 → 971 t/s at 5,904 tokens, 829 → 925 at
19,644) and 2% faster at 59K. Against 0001–0017, the release build before
it (window 29), decode is 41% faster at a short context (33.6 → 47.3 t/s)
and 2.4 times as fast after 59K (15.2 → 36.1 t/s). 0001–0018 is from window
32 and the full series from window 34; each step between them was measured
against its predecessor in one window, below.

Text-only, ctx 196608, q8_0 KV, `-b 2048 -ub 512`, the probes of the
0018 section. Each window compares builds served one after another on the
same day.

Window 29, 2026-11-26:

| build | prefill short / 5,904 / 19,644 / 59,042 | decode short / greedy | decode after 5,904 / 19,644 / 59,042 |
|---|---:|---:|---:|
| 0001–0017 | 735 / 852 / 830 / 708 t/s | 33.6 / 37.1 t/s | 28.9 / 23.5 / 15.2 t/s |
| 0001–0018 | 736 / 852 / 829 / 708 t/s | 37.3 / 37.7 t/s | 34.6 / 32.4 / 27.3 t/s |
| master + 0002–0019 | 801 / 912 / 873 / 704 t/s | 41.9 / 42.2 t/s | 38.1 / 35.4 / 29.2 t/s |

On master, decode rises 12% over 0001–0018 at a short context and 7% after
59K tokens; prefill rises 5–9% below 20K tokens. Both needles were found
on all three builds.

Window 30, 2026-11-27: 0020 adds 4–6% prefill (short 777 → 820 t/s, 5,904
tokens 904 → 949, 19,644 tokens 866 → 906, the 59,042-token fill 696 →
721). Decode is unchanged at 41.9 / 42.0 t/s, and 29.2 / 29.3 after the
fill. The reorder lets the grouped GEMMs read the NVFP4 quants with 2-D
block loads: the MUL_MAT_ID kernel at 512 tokens went from 1,275 to
1,192 µs. The one-token mat-vec stays at 23.2–23.6 µs.

Window 31, 2026-11-27, 0021 and 0022 on 0002–0020 (env toggles of a
diagnostic build; the final code takes the last row's path):

| build | decode short | decode greedy | decode after 5,904 |
|---|---:|---:|---:|
| 0002–0020 | 42.16 / 42.16 t/s | 42.43 / 42.47 t/s | 38.25 / 38.36 t/s |
| + 0022 | 42.66 / 43.08 t/s | 43.39 t/s | 38.88 t/s |
| + 0021 | 43.42 / 43.43 t/s | 43.71 t/s | 39.45 t/s |
| + 0021 + 0022 | 44.42 / 44.43 t/s | 44.74 / 44.76 t/s | 40.15 / 40.27 t/s |

Prefill stayed at 948 t/s at 5,904 tokens in every row. The decode profile
before these patches charged 24.6 µs to each of the 48 bf16
attention-sink projections per token ([2304 × 24], one sub-group per row
in DMMV); 0021 runs it in 7.6 µs. Each of the 88 depth mixes per token
was one 256-item work-group on the device, 26 µs per call; 0022 gives it
1,024 items at decode.

Window 32, 2026-11-27, 0002–0022 against 0001–0018 (#306):

| build | prefill short / 5,904 / 19,644 / 59,042 | decode short / greedy | decode after 5,904 / 19,644 / 59,042 |
|---|---:|---:|---:|
| 0001–0018 | 736 / 852 / 829 / 708 t/s | 37.3 / 37.7 t/s | 34.5 / 32.4 / 27.3 t/s |
| 0002–0022 | 819 / 948 / 904 / 720 t/s | 44.4 / 44.8 t/s | 40.3 / 37.2 / 30.4 t/s |

Decode is 19% faster at a short context, 15% after 19,644 tokens and 11%
after 59,042. Prefill is 11% faster up to 5,904 tokens, 9% at 19,644 and
2% at 59,042. A second 0001–0018 server after the others decoded at
37.32 / 37.33 t/s, so the card did not drift. Both needles were found and
the tool call was parsed on both builds.

Window 33, 2026-11-27, 0023 and 0024 on 0002–0022, two servers each:

| build | prefill short | prefill 5,904 | decode short | decode greedy | decode after 5,904 |
|---|---:|---:|---:|---:|---:|
| 0002–0022 | 814 / 815 t/s | 947.1 / 946.6 t/s | 44.45 / 44.42 t/s | 44.76 / 44.76 t/s | 40.24 / 40.17 t/s |
| + 0023 + 0024 | 830 / 828 t/s | 970.8 / 969.4 t/s | 46.13 / 46.11 t/s | 46.51 / 46.52 t/s | 41.54 / 41.57 t/s |

The two fusions remove about 530 of some 2,650 kernels per token: decode
gains 3.8% and prefill 2.5%. After a 59,054-token fill, prefill was 720.0 t/s
and decode 31.18 t/s (719.6 and 30.39 without them, window 32). The greedy
answer ends at 360 tokens instead of running to the 400-token limit: the
fused reduction adds the experts in the same order but without the
intermediate tensors' rounding, and greedy decoding follows the changed
logits. The KL divergence below measures how far they moved.

Window 34, 2026-11-27, 0025 on 0002–0024, two servers each:

| build | decode short | decode greedy | after 5,904 | after 19,644 | after 59,054 |
|---|---:|---:|---:|---:|---:|
| 0002–0024 | 46.05 / 46.08 t/s | 46.50 / 46.50 t/s | 41.53 / 41.57 t/s | 38.34 / 38.39 t/s | 31.20 t/s |
| + 0025 | 47.28 / 47.29 t/s | 47.59 / 47.63 t/s | 44.63 / 44.55 t/s | 41.87 / 41.86 t/s | 36.05 t/s |

0025 changes only the decode attention kernel, so the gain grows with the
context: 2.7% at a short one, 7.4% after 5,904 tokens, 9.2% after 19,644
and 15.5% after 59,054. The 40 sliding-window layer passes per token run
the same kernel over about 1,000 keys, which is the short-context gain.
Prefill did not move (970 / 971 t/s at 5,904 tokens, 924–925 at 19,644,
721 / 725 in the fill). Both needles were found on both builds. Decode
after 59K tokens is now 24% below decode at 847 tokens, against 31% before
0025.

### Accuracy

KL divergence against the logits of master + 0002–0019 (the base), wiki
text, 512-token chunks, `llama-perplexity --kl-divergence`. The same
build against its own base gives the noise floor:

| build | ub | chunks | mean KLD | 99.9% KLD | same top token |
|---|---:|---:|---:|---:|---:|
| base again (floor) | 512 | 40 | 0.00138 | 0.0570 | 98.35% |
| + 0020 | 512 | 40 | 0.00132 | 0.0459 | 98.18% |
| + 0020–0022 | 512 | 40 | 0.00136 | 0.0578 | 98.45% |
| base again (floor) | 1 | 4 | 0.00217 | 0.0649 | 97.75% |
| + 0020 | 1 | 4 | 0.00229 | 0.1242 | 97.55% |
| + 0020–0022 | 1 | 4 | 0.00140 | 0.0330 | 98.04% |

All rows are at the floor. `-ub 1` runs every token through the decode
kernels, including 0021's: at one token the series lowers the largest
single KLD from 0.549 to 0.058. 0021 accumulates the bf16 weights in F32.

Window 34 measured 0023–0025 against 0002–0022 with the same method:

| build | KV cache | ub | chunks | mean KLD | 99.9% KLD | same top token |
|---|---|---:|---:|---:|---:|---:|
| 0002–0022 again (floor) | f16 | 512 | 40 | 0.00135 | 0.0673 | 98.59% |
| + 0023 + 0024 | f16 | 512 | 40 | 0.00126 | 0.0499 | 98.55% |
| + 0023–0025 | f16 | 512 | 40 | 0.00131 | 0.0531 | 98.57% |
| 0002–0022 again (floor) | f16 | 1 | 4 | 0.00159 | 0.0338 | 97.35% |
| + 0023 + 0024 | f16 | 1 | 4 | 0.00164 | 0.0365 | 97.84% |
| 0002–0024 again (floor), base 0002–0024 | q8_0 | 1 | 4 | 0.00211 | 0.2331 | 97.75% |
| + 0025, base 0002–0024 | q8_0 | 1 | 4 | 0.00186 | 0.0336 | 97.75% |

All rows are at the floor. The decode kernel that 0025 changes handles only
quantized caches. With an f16 cache, 0002–0025 at `-ub 1` gave results
bit-identical to 0002–0024. The q8_0 rows (the production cache type,
against a q8_0 base) run every token through it.

The top-5 logprob probe on the 24.5K-token prompt (window 32, 0002–0022
against 0001–0018) agreed on the first 11 of 32 tokens and split at the
twelfth, the near tie that 0018's probe split at. Before the split the
largest top-5 probability difference was 0.068.

### Vision

Window 31 compared the shipped vision profile (ctx 196608, the F16
projector on the GPU) on 0001–0017 and on master + 0002–0020, twice
each:

| build | 4000x3000 image | wall | decode | 1024x1024 image | wall |
|---|---:|---:|---:|---:|---:|
| 0001–0017 | 589.0 / 589.4 t/s | 10.3 / 10.3 s | 30.2 t/s | 477.5 / 477.4 t/s | 4.3 / 4.3 s |
| master + 0002–0020 | 692.6 / 693.0 t/s | 8.6 / 8.6 s | 39.0 t/s | 555.7 / 552.5 t/s | 4.0 / 3.6 s |

Both images were read correctly in every run.

Windows 30 and 32 sent the 4000x3000 image as the first image after a
59K-token fill and took 18.6–18.8 s on the master pin, against 13.1 s for
0001–0016 in the same probe. Window 33 separated the first image from the
fill, on 0002–0022 and 0001–0018, one server each:

| build | first image after start | second image | after a 58,723-token fill | fill prefill / decode |
|---|---:|---:|---:|---:|
| 0001–0018 | 444.5 t/s, 12.1 s | 596.7 t/s, 9.7 s | 598.1 t/s, 10.3 s | 655.4 / 27.40 t/s |
| 0002–0022 | 496.6 t/s, 10.8 s | 703.8 t/s, 8.4 s | 704.3 t/s, 8.9 s | 719.0 / 30.35 t/s |

The fill does not slow an image down. The first image after a server start
is slow on both builds: it pays the encoder's first kernels and buffers.
In windows 30 and 32 that first image also came straight after the fill,
which made it slower still (about 260 t/s). In steady use the master pin
reads the large image 18% faster than 0001–0018. With the projector loaded
it also fills 10% faster and decodes 11% faster after the fill.
VRAM on 0002–0022 was 21.69 GiB after loading, 21.87 GiB after the fill and
22.28 GiB after the image (21.74, 21.92 and 22.34 GiB on 0001–0016).

### The other Arc models

The pin moves every model to 176 newer upstream commits; the patches that
are new since 0001–0018 only change kernels on Inkling's path. Window 33 served
each with its catalog flags on both builds and repeated each request three
times. The first request of a fresh server compiles that model's kernels
(SYCL is JIT-compiled here), so the table shows the repeats:

| model | prefill 0001–0018 | prefill 0002–0022 | decode 0001–0018 | decode 0002–0022 |
|---|---:|---:|---:|---:|
| Muse Glimmer 30B | 1,056 t/s | 1,056 t/s | 21.76 t/s | 21.76 t/s |
| Qwen3.8-27B Q5_K_M | 886 t/s | 866 t/s | 18.73 t/s | 19.02 t/s |
| flint Q3_K_XL | 1,312 t/s | 1,312 t/s | 62.8 t/s | 63.3 t/s |
| Nemotron 3.5 Bolt (window 32) | 389 t/s | 390 t/s | 28.0 t/s | 28.4 t/s |

Prefill prompts were 7,573–8,342 tokens. Qwen3.8 prefills 2.2% slower and
decodes 1.5% faster. Upstream changed its gated DeltaNet path in this range
(#31762 adds an ESIMD kernel; #31669 replays instead of rolling back), and
the series does not touch it. Glimmer's and Qwen3.8's short answers began
with the same text on both builds.

0024 is for NVFP4 checkpoints only. flint, a MoE with a q8_0 cache at head
size 128, gains from 0023's reduction and 0025's attention kernel. Bolt
runs few attention layers and changed little. Glimmer and Qwen3.8 are
dense, Glimmer runs an f16 cache and Qwen3.8 has head size 256, so neither
fusion nor the new kernel runs for them. Windows 33 and 34, repeats only:

| model | build | prefill | decode short | decode after the prefill probe |
|---|---|---:|---:|---:|
| flint | 0002–0022 | 1,312 t/s | 63.3 t/s | 54.9 t/s |
| flint | 0002–0024 | 1,379 t/s | 64.8 t/s | 56.1 t/s |
| flint | 0002–0025 | 1,379 t/s | 66.6 t/s | 61.1 t/s |
| Bolt | 0002–0024 | 405 t/s | 29.04 t/s | 28.6 t/s |
| Bolt | 0002–0025 | 405 t/s | 29.09 t/s | 28.8 t/s |

The first-request cost is the driver compiling kernels on their first use.
In window 32 the first Qwen3.8 prompt on a never-run binary took 31 s.
The compute runtime caches compiled kernels across server starts, so the
same binary started again in window 33 needed 1.2 s. After an update, each
model's first request compiles once. `SYCL_CACHE_PERSISTENT=1`, SYCL's own
cache, crashed llama-server at startup (SIGSEGV, twice), so the catalog
keeps it at 0.

### Tests

`test-backend-ops` on the B60, on the builds of windows 32–34. MUL_MAT_ID
NVFP4 86/86, MUL_MAT_ID q4_K 114/114, TOPK_MOE 698/698, DEPTH_MIX 22/22,
SHORT_CONV 70/70, LOOP_CTRL 4/4, L2_CLIP 16/16, SITU 4/4, CONT 91/91,
SCALE 7/7, SOFT_MAX 215/215, MOE_REDUCE 8/8 and MUL_MAT_FUSION
1,780/1,780 passed. In FLASH_ATTN_EXT, the q8_0 head-size-128 decode sweep
passed 394/394, the indexed-bias cases 22/22, the GQA-16 q8_0 cases
101/101 and q4_0 351/351.

The fused paths ran in these tests: 7 of the 8 MOE_REDUCE cases (the eighth
uses 16 experts, above the fusion's 15) and 266 MUL_MAT_FUSION cases. With
P·V switched off in a diagnostic build, 292 of the 298 sweep cases failed,
so the sweep reaches 0025's kernel.

Two suites fail on this card for reasons outside the series. MUL_MAT_ID
q8_0 runs out of device memory while production holds 22 GB, and does so
on 0001–0018 too. The f16 FLASH_ATTN_EXT suite aborts in the tile kernel's
dispatch on plain upstream master as well.

The `kv_rows` indexed-bias cases are master's multi-sequence attention;
SYCL reports them unsupported, and llama.cpp falls back to the dense mask.

### What did not help

- Two or four rows per sub-group in the reordered NVFP4 MoE mat-vec, for
  fewer launches and shared activations: gate_up went from 79.9 to
  81.9 µs (two rows) and 82.9 µs (four), and decode from 42.2 to 41.7 and
  41.5 t/s. One row per sub-group stays.
- More rows per work-group in the MoE mat-vec (2, 4 and 8 sub-groups):
  79.8–81.1 µs against 79.8 µs at one, so occupancy does not limit it.
- For the decode attention kernel: 128-key tiles (1,164–1,358 µs at 65,536
  keys against 854 µs for 0025's kernel in the same diagnostic build),
  issuing all of a tile's staging loads before its SLM stores (1,453 against
  1,310 µs), and an extra L1 prefetch of the next K (neutral). Dropping the
  old kernel's L1 prefetches made it slower (2,063 against 1,433 µs). The
  prefetches were doing what 0025's early loads now do.

### Remaining decode costs

The profile before 0021 and 0022 ran about 2,650 kernels per token, and
0023 and 0024 remove about 530 of them. The NVFP4 MoE mat-vecs are the
largest share, 16.5%, at about half of the card's 456 GB/s. The q8_0 output
projection runs at 424 GB/s and has no headroom. After 0025 the decode
attention kernel reaches 160 GB/s at 65,536 keys, so long-context decode
still has room there.
