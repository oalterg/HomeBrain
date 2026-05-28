#!/usr/bin/env bash
# 27B MTP cliff localization + tuning
# Pass 1: fine ctx sweep to locate where MTP acceleration cuts out (between 32K and 49K)
# Pass 2 (PASS=2): tune KV-type / -ub / n_max around the located cliff
set +e

PASS="${1:-1}"
LLAMA_DIR="$HOME/ai-runtime/llama-server"
BENCH_DIR="$HOME/bench-upgrade"
MODEL_DIR="$HOME/models"
PORT=8099
RESULTS="$BENCH_DIR/cliff-pass${PASS}.jsonl"

MTP_27B="$MODEL_DIR/Qwen3.6-27B-MTP-IQ4_XS.gguf"
BASE_27B="$MODEL_DIR/Qwen3.6-27B-IQ4_XS.gguf"

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
    while [ "$(vram_mb)" -gt 500 ] && [ "$i" -lt 25 ]; do sleep 1; i=$((i+1)); done
}

# Predictable probe (high MTP acceptance) — reuse existing if present
PROBE="$BENCH_DIR/probe_predictable.json"
if [ ! -f "$PROBE" ]; then
python3 -c "
import json
print(json.dumps({
    'prompt': '<|im_start|>user\nRepeat the following pattern exactly 100 times: apple banana cherry dog elephant frog grape hat ice jam kite lemon mango nest orange pear queen rose sun tree umbrella vine water xray yak zebra<|im_end|>\n<|im_start|>assistant\n',
    'n_predict': 512, 'temperature': 0.0, 'top_p': 1.0, 'cache_prompt': False
}))
" > "$PROBE"
fi

probe() {
    local resp
    resp=$(curl -sf --max-time 180 -H "Content-Type: application/json" -d @"$PROBE" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null)
    [ -z "$resp" ] && { echo "0 0"; return 1; }
    python3 -c "
import sys, json
r = json.loads(sys.stdin.read())
t = r.get('timings', {})
tg = t.get('predicted_per_second', 0)
n = t.get('predicted_n', 0)
print(f'{tg} {n}')
" <<< "$resp" 2>/dev/null || echo "0 0"
}

run_cell() {
    local label="$1" model="$2" ctx="$3" kv="$4" ub="$5"; shift 5
    local extra=("$@")
    stop_all
    log "--- $label | ctx=$ctx kv=$kv ub=$ub ${extra[*]} ---"
    RADV_PERFTEST=rm_kq=1 "$LLAMA_DIR/llama-server" \
        --model "$model" --ctx-size "$ctx" \
        --host 127.0.0.1 --port "$PORT" --parallel 1 -ngl 99 \
        -fa on --cache-type-k "$kv" --cache-type-v "$kv" \
        -t 6 -b 4096 -ub "$ub" "${extra[@]}" \
        > "$BENCH_DIR/server.log" 2>&1 &
    local pid=$! i=0
    while [ "$i" -lt 200 ]; do
        kill -0 "$pid" 2>/dev/null || { log "  DIED"; tail -3 "$BENCH_DIR/server.log"; return 1; }
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 1; i=$((i+1))
    done
    [ "$i" -ge 200 ] && { log "  TIMEOUT"; kill "$pid" 2>/dev/null; return 1; }
    local vram=$(vram_mb)
    log "  Healthy (${i}s, VRAM ${vram} MiB, headroom $((16304 - vram)) MiB)"
    curl -sf --max-time 60 -H "Content-Type: application/json" -d @"$PROBE" \
        "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    local tg_vals=""
    for run in 1 2 3; do
        read tg_ts tg_n <<< "$(probe)"
        log "    Run $run: $tg_ts t/s ($tg_n tok)"
        tg_vals="$tg_vals $tg_ts"
    done
    python3 -c "
import json, statistics
vals=[float(x) for x in '$tg_vals'.split() if float(x)>0]
r={'label':'$label','ctx':$ctx,'kv':'$kv','ub':$ub,'vram_mb':$vram,'headroom_mb':$((16304-vram)),
   'tg_avg':round(statistics.mean(vals),2) if vals else 0,
   'tg_stdev':round(statistics.stdev(vals),2) if len(vals)>1 else 0,'runs':len(vals)}
open('$RESULTS','a').write(json.dumps(r)+'\n')
print(f\"  => {r['tg_avg']:.2f} ± {r['tg_stdev']:.2f} t/s (headroom {r['headroom_mb']} MiB)\")
"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
}

log "27B MTP cliff investigation — PASS $PASS (b9186)"

if [ "$PASS" = "1" ]; then
    # Locate the cliff: fine ctx sweep, MTP n=2, q4 KV, ub2048
    for ctx in 32768 36864 40960 45056 49152; do
        run_cell "MTP-n2-c${ctx}" "$MTP_27B" "$ctx" q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
    done
    # Base sanity at the cliff zone (expect ~17 flat)
    run_cell "base-c40960" "$BASE_27B" 40960 q4_0 2048
else
    # PASS 2 — cliff is at exactly 32768 (Pass 1). Pin the edge + tune for max perf at/below it.
    # (A) Edge: does ANY ctx between 32768 and 36864 still accelerate?
    run_cell "MTP-n2-c33792" "$MTP_27B" 33792 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
    run_cell "MTP-n2-c34816" "$MTP_27B" 34816 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
    # (B) n_max sweep at 32768 (never tested BELOW the cliff before; 35B liked n=3)
    run_cell "MTP-n1-c32768" "$MTP_27B" 32768 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 1
    run_cell "MTP-n3-c32768" "$MTP_27B" 32768 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 3
    run_cell "MTP-n4-c32768" "$MTP_27B" 32768 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 4
    # (C) KV q8 at 32768 — better attention numerics; does it still fit and hold speed?
    run_cell "MTP-n2-c32768-q8" "$MTP_27B" 32768 q8_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
    run_cell "MTP-n3-c32768-q8" "$MTP_27B" 32768 q8_0 2048 --spec-type draft-mtp --spec-draft-n-max 3
    # (D) realistic prompt at best config (code) — predictable is upper bound
    PROBE="$BENCH_DIR/probe_code.json"
    run_cell "MTP-n3-c32768-code" "$MTP_27B" 32768 q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 3
    PROBE="$BENCH_DIR/probe_predictable.json"
fi

log "Results in $RESULTS"
python3 -c "
import json
rows=[json.loads(l) for l in open('$RESULTS')]
print('| Label | Ctx | KV | ub | TG | Headroom |')
print('|---|--:|--|--:|--:|--:|')
for r in rows:
    print(f\"| {r['label']} | {r['ctx']} | {r['kv']} | {r['ub']} | {r['tg_avg']:.2f} ± {r['tg_stdev']:.2f} | {r['headroom_mb']} |\")
"

# Restart production (sweep killed it)
log "Restarting production 35B Q5_K_XL..."
stop_all
RADV_PERFTEST=rm_kq=1 nohup "$LLAMA_DIR/llama-server" \
    --model "$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf" \
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
