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

- **27B IQ4_XS:** ships `--spec-type draft-mtp --spec-draft-n-max 2` at ctx=49152 — i.e. it pays for the MTP GGUF and gets ≈+2% for it (49152 is over the cliff). Dropping `context_window` 49152 → 32768 (keeping n_max=2 + q4 KV) unlocks **+75% TG (17.1 → ~30 t/s)** at the cost of 16K context. For the home-automation agent 32K is likely ample; **worth a decision.** Do **not** also raise n_max or switch to q8 KV — either change re-crosses the speculative ceiling and collapses TG back to ~17. If 49K context must stay, MTP buys nothing there and the plain IQ4_XS GGUF would do. See [27B cliff localization & parameter tuning](#27b-cliff-localization--parameter-tuning-b9186-2026-05-28).
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

> **Correction (2026-05-30):** the MTP entry originally shipped with **q8 KV**, which proved unstable (see "compute-buffer eviction" below) — the +50% decayed to break-even under real use. The shipped config was changed to **q4_0 KV** (`--cache-type-k/v q4_0`), keeping ctx 131072 and delivering a *sustained* +50% greedy / +31% real. The VRAM-settle gate and `verify_llama_allocation` guard were added in the same change.

### Compute-buffer eviction — why the q8/131072 win wasn't real, and the q4 fix (found 2026-05-29/30)

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

**Fix: headroom.** Moving more experts to CPU (`-ot blk.18-39`, `16-39`) did **not** help — at ctx 131072 the full KV still evicts the buffer regardless of expert placement. Only reducing the KV footprint does. Stress-sweep (fresh → 27K stress → sustained):

| Config | fresh | sustained (post-stress) | VRAM after stress | verdict |
|---|---:|---:|---:|---|
| q8 / ctx 131072 (original) | 43.1 | **30.8** | 15518 (evicts) | ❌ decays |
| q8 / ctx 65536 | 43.1 | **43.0** | 15785 (stable) | ✅ half context |
| **q4 / ctx 131072** | 43.0 | **43.0** | 15645 (stable) | ✅ **shipped** |

**q4_0 KV** frees ~640 MiB — enough that the compute buffer stays resident through full-context growth — while **keeping the full 131072 window**. Sustained 43 t/s greedy / ~37 real, verified to survive the exact stress that collapses q8. The q4 attention-precision cost is modest and acceptable for the agent workload; q8/65536 is the alternative if max numerics matter more than context length.

**Guards also shipped (defense-in-depth for the startup case):**
- `config/llama-server.service` — `ExecStartPre` **VRAM-settle gate**: before allocating, wait (≤120 s) until non-llama VRAM use drains below 300 MiB (whisper idle ≈ 56 MiB), so a model-switch's old instance has fully released VRAM first.
- `scripts/utilities.sh` `verify_llama_allocation()` — after the health check on the model-switch/update path, if the process came up below the model's `min_healthy_vram_mb` watermark, restart (the gate holds the retry until VRAM drains) and re-check, up to 2×; never fails the caller, logs loudly.
- `config/platform_models.json` — the MTP entry declares `"min_healthy_vram_mb": 15300` (below the ~15645 stable reading, above a starved start). Models without the field skip the check.
