# HomeBrain AI Inference Benchmarks

## Hardware

| Component | Spec |
|-----------|------|
| GPU       | AMD Radeon RX 9060 XT (16 GB VRAM) |
| CPU       | Intel Core i5 6-core |
| RAM       | DDR4-2400 |
| Backend   | Vulkan (RADV / GFX1200) |
| OS        | Ubuntu 24.04 (x86_64) |
| llama.cpp | b8931 |

## Methodology

- TG = text generation throughput (tokens/sec), 256-token completion, ~26-token prompt
- PP = prompt processing throughput (tokens/sec), measured at ~2.4k-token prompt
- Target throughput for production use: 20 t/s (text generation)
- Captured via `/v1/chat/completions` `timings.predicted_per_second` / `prompt_per_second`

## Results

| Model                   | Quant     | Total | Ctx  | KV     | -ot range | -b/-ub      | TG (t/s) | PP@2k (t/s) | Date       |
|-------------------------|-----------|-------|------|--------|-----------|-------------|----------|-------------|------------|
| **Qwen3.6-35B-A3B**     | UD-Q5_K_XL| 35B   | 131K | q8_0   | 20-39     | 4096 / 2048 | **29.67**| **574**     | 2026-04-25 |
| Qwen3.6-35B-A3B         | UD-Q6_K   | 35B   | 131K | q8_0   | 18-39     | 4096 / 2048 | 27.18    | 513         | 2026-04-25 |
| Qwen3.6-35B-A3B         | UD-Q8_K_XL| 35B   | 131K | q8_0   | 14-39     | 4096 / 2048 | 18.03    | 140         | 2026-04-25 |
| Qwen3.6-27B (DeltaNet)  | IQ4_XS    | 27B   | 64K  | q4_0   | (none)    | 2048 / 1024 | 14.90    | 437 (@600)  | 2026-04-25 |

Q5_K_XL is the production default — best TG, lowest VRAM pressure, and quality close to Q6_K.

## Tuning Log

| Model     | Run                                | TG    | PP@2k  | VRAM used | Verdict |
|-----------|------------------------------------|-------|--------|-----------|---------|
| Q5_K_XL   | baseline (-ot 20-39, -b/-ub 2048/1024) | 29.85 | 491.9  | 16,541 MB | OK |
| Q5_K_XL   | -b 4096 -ub 2048                   | 29.67 | 574.7  | ~16,500 MB| **+17% PP, TG flat — kept** |
| Q6_K      | baseline (-ot 24-39, -b/-ub 2048/1024) | 14.60 | 138.9  | 17,010 MB | VRAM 99.5%, thrashing |
| Q6_K      | -ot 20-39 -b 4096 -ub 2048         | 22.18 | 139.1  | 16,917 MB | Better TG, PP still throttled |
| Q6_K      | -ot 18-39 -b 4096 -ub 2048         | 27.18 | 513.9  | 16,330 MB | **+86% TG, +270% PP — kept** |
| Q8_K_XL   | baseline (-ot 18-39, Q6_K params)  | 11.35 | 133    | 16,963 MB | VRAM 99%, weight-bound |
| Q8_K_XL   | -ot 12-39 -b 4096 -ub 2048         | 17.24 | 149    | 16,559 MB | More CPU offload, +52% TG |
| Q8_K_XL   | -ot 14-39 -b 4096 -ub 2048         | 18.03 | 140    | 16,473 MB | **Best TG — kept**; below 20 t/s target |
| Q8_K_XL   | -ot 10-39 -b 4096 -ub 2048         | 16.85 | 149    | 16,877 MB | More on CPU hurts TG |
| Q8_K_XL   | -ot 15-39 -b 4096 -ub 2048         | 18.17 | 133    | 16,975 MB | TG matches 14-39, PP worse |

Tuning principles confirmed on this hardware:
- VRAM headroom matters more than maximizing on-GPU layers. At >97% VRAM use, both TG and PP collapse from allocation thrashing — moving 1-2 more blocks to CPU recovers everything.
- `-b 4096 -ub 2048` improves PP ~17% on long prompts vs `-b 2048 -ub 1024`, with no TG cost.
- For Q6_K (29 GB), `-ot` must offload at least blocks 18-39 (22 of 40); for Q5_K_XL (25 GB), 20-39 (20 of 40) is enough; for Q8_K_XL (38 GB), 14-39 (26 of 40) — below 20 t/s on this CPU, so Q8 is not recommended for production.
- 27B IQ4_XS uses no `-ot` (DeltaNet hybrid — partial offload breaks fused kernel).

## Notes

### AMD Vulkan / GFX1200 constraints
- `--cache-type-v q8_0` without `-fa` fails context creation on AMD Vulkan — flash attention is mandatory with quantized KV cache.
- DeltaNet hybrids (Qwen3.6-27B) need `-ngl 99` and no expert offload — `nkvo` and partial offload break the fused kernel.
- `amdgpu.runpm=0` (set via `config/99-amdgpu-runpm.rules`) must remain to prevent VRAM eviction during inference.

### Qwen3.6-35B-A3B MoE specifics
- Architecture: 40 layers, 256 experts (8 active per token), n_embd=2048, n_head=16, n_head_kv=2.
- KV cache at 131K ctx with q8_0: ~1.4 GB on GPU.
- Q5_K_XL stays under VRAM limit at -ot 20-39; Q6_K needs 18-39 to leave ~700 MB headroom for compute buffers; Q8_K_XL needs 14-39 (26 of 40 layers on CPU) and is CPU-bound.
