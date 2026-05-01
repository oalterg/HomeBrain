# HomeBrain AI Inference Benchmarks

## Hardware

| Component | Spec |
|-----------|------|
| GPU       | AMD Radeon RX 9060 XT (16 GB VRAM, 16,304 MiB usable) |
| CPU       | AMD Ryzen 5 5600 (6 cores / 12 threads) |
| RAM       | 32 GB DDR4 |
| Backend   | Vulkan (RADV / GFX1200) |
| OS        | Ubuntu 24.04 (x86_64) |
| llama.cpp | b8996 (upgraded from b8951; non-Q5_K_XL/Q4 rows in this table still measured on b8951) |
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
| Qwen3.6-27B (DeltaNet)  | IQ4_XS     | 14 GB | 32K  | q8_0 | (none)    | 4096 / 2048 | **17.22**| 436         | 16.1 GB  | 2026-05-01 |

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
| **ctx 32K + KV q8_0 + b 4096 / ub 2048**  |  32K| q8_0 | 4096 / 2048 | **17.22** | 436 | 16.15 GB | **Kept — best TG, ctx 32K, q8_0 KV** |

Net win on the 27B: **+15.6% TG (14.89 → 17.22), PP flat (442 → 436), KV upgraded q4_0 → q8_0** at the cost of half the context window (64K → 32K). Same VRAM-pressure pattern as the 35B-A3B quants — DeltaNet's TG was VRAM-bound, not kernel-bound, just less obvious because the fused kernel masked the regression.

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
