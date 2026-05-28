#!/usr/bin/env bash
# MTP deep-dive: sweep n_max, prompts, VRAM headroom
# Tests 27B-IQ4_XS MTP and 35B-A3B Q5_K_XL MTP

set +e

TAG="${1:-b9186}"
LLAMA_DIR="$HOME/ai-runtime/llama-server"
BENCH_DIR="$HOME/bench-upgrade"
MODEL_DIR="$HOME/models"
PORT=8099
RESULTS="$BENCH_DIR/mtp-sweep.jsonl"

VRAM_PATH=""
for p in /sys/class/drm/card1/device/mem_info_vram_used /sys/class/drm/card0/device/mem_info_vram_used; do
    [ -f "$p" ] && VRAM_PATH="$p" && break
done

log() { echo "[$(date +%H:%M:%S)] $*"; }
vram_mb() { echo $(( $(cat "$VRAM_PATH") / 1048576 )); }

mkdir -p "$BENCH_DIR"
> "$RESULTS"

stop_all() {
    pkill -x llama-server 2>/dev/null || true
    sleep 3
    pkill -9 -x llama-server 2>/dev/null || true
    sleep 2
    local i=0
    while [ "$(vram_mb)" -gt 500 ] && [ "$i" -lt 20 ]; do sleep 1; i=$((i+1)); done
}

# --- Generate probe files ---
# Probe A: highly predictable (repetitive pattern) — should get HIGH acceptance
python3 -c "
import json
print(json.dumps({
    'prompt': '<|im_start|>user\nRepeat the following pattern exactly 100 times: apple banana cherry dog elephant frog grape hat ice jam kite lemon mango nest orange pear queen rose sun tree umbrella vine water xray yak zebra<|im_end|>\n<|im_start|>assistant\n',
    'n_predict': 512, 'temperature': 0.0, 'top_p': 1.0, 'cache_prompt': False
}))
" > "$BENCH_DIR/probe_predictable.json"

# Probe B: code (moderately predictable) — should get MEDIUM acceptance
python3 -c "
import json
print(json.dumps({
    'prompt': '<|im_start|>user\nWrite a Python function that implements merge sort with detailed comments on every line. Include a main block that tests it with a list of 50 random numbers.<|im_end|>\n<|im_start|>assistant\n',
    'n_predict': 512, 'temperature': 0.0, 'top_p': 1.0, 'cache_prompt': False
}))
" > "$BENCH_DIR/probe_code.json"

# Probe C: creative (unpredictable) — should get LOW acceptance
python3 -c "
import json
print(json.dumps({
    'prompt': '<|im_start|>user\nWrite an original short story about a detective who discovers their own reflection has been committing crimes. Make it surprising and unpredictable.<|im_end|>\n<|im_start|>assistant\n',
    'n_predict': 512, 'temperature': 0.0, 'top_p': 1.0, 'cache_prompt': False
}))
" > "$BENCH_DIR/probe_creative.json"

# --- Probe function using /completion (bypasses chat template + thinking) ---
probe() {
    local probe_file="$1"
    local resp
    resp=$(curl -sf --max-time 180 \
        -H "Content-Type: application/json" \
        -d @"$probe_file" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null)

    if [ -z "$resp" ]; then
        echo "0 0 0"
        return 1
    fi

    python3 -c "
import sys, json
r = json.loads(sys.stdin.read())
t = r.get('timings', {})
tg = t.get('predicted_per_second', 0)
n = t.get('predicted_n', 0)
# n_accepted from MTP stats if available
print(f'{tg} {n}')
" <<< "$resp" 2>/dev/null || echo "0 0"
}

# --- Start server and run probes ---
run_cell() {
    local label="$1" model="$2" ctx="$3" probe_name="$4" probe_file="$5"
    shift 5
    local extra_flags=("$@")

    stop_all

    log "--- $label | ctx=$ctx | probe=$probe_name ---"

    local bin_dir="$LLAMA_DIR"

    RADV_PERFTEST=rm_kq=1 "$bin_dir/llama-server" \
        --model "$model" \
        --ctx-size "$ctx" \
        --host 127.0.0.1 --port "$PORT" \
        --parallel 1 -ngl 99 \
        -fa on --cache-type-k q4_0 --cache-type-v q4_0 \
        -t 6 -b 4096 -ub 2048 \
        "${extra_flags[@]}" \
        > "$BENCH_DIR/server.log" 2>&1 &
    local pid=$!

    # Wait healthy
    local i=0
    while [ "$i" -lt 180 ]; do
        kill -0 "$pid" 2>/dev/null || { log "  DIED"; tail -3 "$BENCH_DIR/server.log"; return 1; }
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 1; i=$((i+1))
    done
    [ "$i" -ge 180 ] && { log "  TIMEOUT"; kill "$pid" 2>/dev/null; return 1; }

    local vram=$(vram_mb)
    log "  Healthy (${i}s, VRAM: ${vram} MiB, headroom: $((16304 - vram)) MiB)"

    # Warmup
    curl -sf --max-time 60 \
        -H "Content-Type: application/json" \
        -d @"$probe_file" \
        "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1

    # 3 measured runs
    local tg_vals=""
    for run in 1 2 3; do
        read tg_ts tg_n <<< "$(probe "$probe_file")"
        log "    Run $run: $tg_ts t/s ($tg_n tok)"
        tg_vals="$tg_vals $tg_ts"
    done

    # Extract MTP acceptance from server log (if available)
    local accept_info
    accept_info=$(grep -o 'accept=[0-9.]*' "$BENCH_DIR/server.log" | tail -1 || echo "")
    [ -n "$accept_info" ] && log "    MTP $accept_info"

    # Summarize
    python3 -c "
import json, statistics
vals = [float(x) for x in '$tg_vals'.split() if float(x) > 0]
result = {
    'label': '$label',
    'probe': '$probe_name',
    'ctx': $ctx,
    'vram_mb': $vram,
    'headroom_mb': $((16304 - vram)),
    'tg_avg': round(statistics.mean(vals), 2) if vals else 0,
    'tg_stdev': round(statistics.stdev(vals), 2) if len(vals) > 1 else 0,
    'runs': len(vals)
}
with open('$RESULTS', 'a') as f:
    f.write(json.dumps(result) + '\n')
avg = result['tg_avg']
sd = result['tg_stdev']
print(f'  => {avg:.2f} ± {sd:.2f} t/s  (VRAM {$vram} MiB, headroom {$((16304 - vram))} MiB)')
"

    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
}

# ============================================================
log "MTP Deep Dive (llama.cpp $TAG)"
log "========================================"

MTP_27B="$MODEL_DIR/Qwen3.6-27B-MTP-IQ4_XS.gguf"
BASE_27B="$MODEL_DIR/Qwen3.6-27B-IQ4_XS.gguf"
MTP_35B="$MODEL_DIR/Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL.gguf"
BASE_35B="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf"

# ============================================================
# PART 1: 27B — Prompt sweep (base vs MTP, 3 prompt types)
# ============================================================
log ""
log "===== PART 1: 27B Prompt Sweep ====="

for probe_name in predictable code creative; do
    probe_file="$BENCH_DIR/probe_${probe_name}.json"

    # Base (no MTP)
    run_cell "27B-base" "$BASE_27B" 49152 "$probe_name" "$probe_file"

    # MTP n=2
    run_cell "27B-MTP-n2" "$MTP_27B" 49152 "$probe_name" "$probe_file" \
        --spec-type draft-mtp --spec-draft-n-max 2
done

# ============================================================
# PART 2: 27B — n_max sweep (best prompt from part 1)
# ============================================================
log ""
log "===== PART 2: 27B n_max Sweep (predictable prompt) ====="

for nmax in 1 2 3 4; do
    run_cell "27B-MTP-n${nmax}" "$MTP_27B" 49152 "predictable" "$BENCH_DIR/probe_predictable.json" \
        --spec-type draft-mtp --spec-draft-n-max "$nmax"
done

# ============================================================
# PART 3: 27B — VRAM headroom sweep (ctx size)
# ============================================================
log ""
log "===== PART 3: 27B VRAM Headroom (MTP n=2, predictable) ====="

for ctx in 8192 16384 32768 49152; do
    run_cell "27B-MTP-ctx${ctx}" "$MTP_27B" "$ctx" "predictable" "$BENCH_DIR/probe_predictable.json" \
        --spec-type draft-mtp --spec-draft-n-max 2
done

# ============================================================
# PART 4: 35B Q5_K_XL — MTP test
# ============================================================
log ""
log "===== PART 4: 35B Q5_K_XL MTP ====="

# Base
run_cell "35B-base" "$BASE_35B" 32768 "predictable" "$BENCH_DIR/probe_predictable.json" \
    -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU"

# MTP n=2
run_cell "35B-MTP-n2" "$MTP_35B" 32768 "predictable" "$BENCH_DIR/probe_predictable.json" \
    -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    --spec-type draft-mtp --spec-draft-n-max 2

# MTP n=3
run_cell "35B-MTP-n3" "$MTP_35B" 32768 "predictable" "$BENCH_DIR/probe_predictable.json" \
    -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    --spec-type draft-mtp --spec-draft-n-max 3

# ============================================================
# SUMMARY
# ============================================================
log ""
log "========================================"
log "Full results in $RESULTS"

python3 << 'PYEOF'
import json
from collections import defaultdict

with open("/home/homebrain/bench-upgrade/mtp-sweep.jsonl") as f:
    rows = [json.loads(l) for l in f]

print()
print("| Label | Probe | Ctx | TG (t/s) | VRAM | Headroom |")
print("|-------|-------|-----|----------|------|----------|")
for r in rows:
    print(f"| {r['label']:20s} | {r['probe']:12s} | {r['ctx']:5d} | {r['tg_avg']:6.2f} ± {r['tg_stdev']:4.2f} | {r['vram_mb']:5d} | {r['headroom_mb']:5d} |")
print()

# Compute MTP gains
base_by_probe = {}
for r in rows:
    if 'base' in r['label'] and '27B' in r['label']:
        base_by_probe[r['probe']] = r['tg_avg']

print("27B MTP gains by prompt type:")
for r in rows:
    if 'MTP-n2' in r['label'] and '27B' in r['label'] and r['probe'] in base_by_probe:
        base = base_by_probe[r['probe']]
        if base > 0:
            gain = ((r['tg_avg'] - base) / base) * 100
            print(f"  {r['probe']:12s}: {base:.2f} → {r['tg_avg']:.2f} ({gain:+.1f}%)")
PYEOF

# --- Restart production ---
log ""
log "Restarting production..."
stop_all

Q5_XL="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf"
RADV_PERFTEST=rm_kq=1 nohup "$LLAMA_DIR/llama-server" \
    --model "$Q5_XL" \
    --ctx-size 131072 --host 127.0.0.1 --port 8001 \
    --parallel 1 --jinja -ngl 99 \
    -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    -fa 1 --cache-type-k q8_0 --cache-type-v q8_0 \
    --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
    --presence-penalty 1.5 --reasoning-budget 8192 \
    -b 4096 -ub 4096 --threads 6 \
    --chat-template-kwargs '{"preserve_thinking": true}' \
    > /tmp/llama-server-restart.log 2>&1 &

log "Production restarting (PID $!). Done."
