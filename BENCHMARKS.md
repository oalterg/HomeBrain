# HomeBrain AI Inference Benchmarks

## Hardware

| Component | Spec |
|-----------|------|
| GPU       | AMD Radeon RX 9060 XT (16 GB VRAM) |
| CPU       | Intel Core i5 6-core |
| RAM       | DDR4-2400 |
| Backend   | Vulkan (RADV / GFX1200) |
| OS        | Ubuntu 24.04 (x86_64) |

## Methodology

- All benchmarks at **131K context** unless noted otherwise
- **TG** = text generation throughput (tokens/sec), measured at tg128
- **PP** = prompt processing throughput (tokens/sec), measured at pp512
- Target throughput for production use: **20 T/s** (text generation)

## Results

| Model | Quant | Active/Total | Ctx | KV Cache | Flags | TG (t/s) | PP (t/s) | Date | Notes |
|-------|-------|-------------|-----|----------|-------|----------|----------|------|-------|
| Qwen3.5-27B | IQ4_XS | 27B/27B | 65K | q8_0/q8_0 | -fa on -b 2048 -ub 512 -t 6 | 14.20 | 385 | 2026-04 | DeltaNet SSM+attn; ngl=99 required; ctx capped at 65K (ctv q8_0 saves 640 MiB) |
| Qwen3.5-35B-A3B | Q4_K_M | 3B/35B | 131K | q8_0/q8_0 | -fa on -b 2048 -ub 128 -ot blk.24+ CPU -t 6 | 28.66 | 165 | 2026-04 | MoE; blk.24+ exps on CPU; fa+ctk/ctv q8_0 confirmed on GFX1200/RADV |
| Qwen3.6-35B-A3B | UD-Q4_K_M | ?B/35B | 131K | TBD | --fit on -fa 1 -b 4096 -ub 2048 | TBD | TBD | TBD | Target: 20 t/s; preserve_thinking=true |

## Tuning Log

Track parameter experiments here. Each row is one run; keep the best result in the main table above.

| Model | Run | Change vs baseline | TG (t/s) | PP (t/s) | Verdict |
|-------|-----|--------------------|----------|----------|---------|
| Qwen3.6-35B-A3B | baseline | --fit on -fa 1 -b 4096 -ub 2048 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 | TBD | TBD | -- |

## Notes

### AMD Vulkan constraints
- `ctv q8_0` without flash attention (`-fa`) fails context creation on AMD Vulkan -- fa is always required when using quantized KV cache
- DeltaNet models (Qwen3.5-27B) require ngl=99; partial offload breaks fused kernel (output degrades to ~2 t/s)
- `nkvo` (no KV offload) breaks DeltaNet fused kernel -- recurrent state must stay GPU-local
- `amdgpu.runpm=0` must remain set to prevent VRAM eviction during inference

### Qwen3.5-35B-A3B MoE tuning
- Expert offload: blk.24+ exps on CPU via `-ot "blk.(2[4-9]|[3-9][0-9]).ffn_.*exps=CPU"`
- Group C (fa=on + ctk/ctv q8_0): pp512=165 t/s (+7.2%), tg128=28.66 t/s (+3.8% vs baseline)
