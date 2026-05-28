#!/usr/bin/env bash
# Production-faithful confirmation: does 35B-A3B MTP help on b9381 with the REAL
# production config (q8 KV, ub4096, -ot 20-39, ctx 131072, full prod samplers)?
# Measures greedy (upper-bound acceptance) AND prod-sampler (realistic) TG.
set +e

BENCH_DIR="$HOME/bench-upgrade"
MODEL_DIR="$HOME/models"
PORT=8099
RESULTS="$BENCH_DIR/mtp35b-prod.jsonl"
BIN="$BENCH_DIR/b9381/llama-b9381"
BASE_35B="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf"
MTP_35B="$MODEL_DIR/Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL.gguf"

VRAM_PATH=""
for p in /sys/class/drm/card1/device/mem_info_vram_used /sys/class/drm/card0/device/mem_info_vram_used; do
    [ -f "$p" ] && VRAM_PATH="$p" && break
done
log() { echo "[$(date +%H:%M:%S)] $*"; }
vram_mb() { echo $(( $(cat "$VRAM_PATH") / 1048576 )); }
mkdir -p "$BENCH_DIR"; > "$RESULTS"
stop_all() {
    pkill -x llama-server 2>/dev/null || true; sleep 3
    pkill -9 -x llama-server 2>/dev/null || true; sleep 2
    local i=0; while [ "$(vram_mb)" -gt 500 ] && [ "$i" -lt 25 ]; do sleep 1; i=$((i+1)); done
}

# greedy probe (deterministic, max acceptance) — predictable pattern
GREEDY="$BENCH_DIR/probe_greedy35.json"
python3 -c "
import json
print(json.dumps({'prompt':'<|im_start|>user\nList the integers from 1 to 200 separated by commas.<|im_end|>\n<|im_start|>assistant\n',
                  'n_predict':512,'temperature':0.0,'top_p':1.0,'cache_prompt':False}))
" > "$GREEDY"
# realistic probe — natural task, NO sampler override (uses server prod sampler temp=1.0 etc.)
REAL="$BENCH_DIR/probe_real35.json"
python3 -c "
import json
print(json.dumps({'prompt':'<|im_start|>user\nWrite a detailed Python module that implements a thermostat controller with hysteresis, scheduling, and a small test suite. Include docstrings.<|im_end|>\n<|im_start|>assistant\n',
                  'n_predict':512,'cache_prompt':False}))
" > "$REAL"

measure() {
    local probe="$1" resp
    resp=$(curl -sf --max-time 240 -H "Content-Type: application/json" -d @"$probe" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null)
    [ -z "$resp" ] && { echo "0 0"; return 1; }
    python3 -c "
import sys, json
r=json.loads(sys.stdin.read()); t=r.get('timings',{})
print(f\"{t.get('predicted_per_second',0)} {t.get('predicted_n',0)}\")
" <<< "$resp" 2>/dev/null || echo "0 0"
}

run_cell() {
    local label="$1" model="$2"; shift 2
    local spec=("$@")
    stop_all
    log "--- $label ${spec[*]} ---"
    RADV_PERFTEST=rm_kq=1 "$BIN/llama-server" \
        --model "$model" --ctx-size 131072 --host 127.0.0.1 --port "$PORT" \
        --parallel 1 --jinja -ngl 99 -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
        -fa on --cache-type-k q8_0 --cache-type-v q8_0 \
        --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
        --presence-penalty 1.5 --reasoning-budget 8192 \
        -b 4096 -ub 4096 --threads 6 \
        --chat-template-kwargs '{"preserve_thinking": true}' \
        "${spec[@]}" > "$BENCH_DIR/server.log" 2>&1 &
    local pid=$! i=0
    while [ "$i" -lt 240 ]; do
        kill -0 "$pid" 2>/dev/null || { log "  DIED"; tail -6 "$BENCH_DIR/server.log"; \
            python3 -c "open('$RESULTS','a').write('{\"label\":\"$label\",\"status\":\"DIED\"}\n')"; return 1; }
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 1; i=$((i+1))
    done
    [ "$i" -ge 240 ] && { log "  TIMEOUT"; kill "$pid" 2>/dev/null; return 1; }
    local vram=$(vram_mb)
    log "  Healthy (${i}s, VRAM ${vram} MiB)"
    curl -sf --max-time 240 -d @"$GREEDY" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    local g_vals="" r_vals=""
    for run in 1 2 3; do
        read g _ <<< "$(measure "$GREEDY")"
        read r _ <<< "$(measure "$REAL")"
        log "    run $run: greedy=$g  real=$r t/s"
        g_vals="$g_vals $g"; r_vals="$r_vals $r"
    done
    # MTP acceptance from log
    local acc; acc=$(grep -oiE 'accept[^ ]*[=: ][0-9.]+' "$BENCH_DIR/server.log" | tail -1)
    [ -n "$acc" ] && log "    $acc"
    python3 -c "
import json, statistics
g=[float(x) for x in '$g_vals'.split() if float(x)>0]
r=[float(x) for x in '$r_vals'.split() if float(x)>0]
o={'label':'$label','vram_mb':$vram,
   'greedy_avg':round(statistics.mean(g),2) if g else 0,
   'greedy_sd':round(statistics.stdev(g),2) if len(g)>1 else 0,
   'real_avg':round(statistics.mean(r),2) if r else 0,
   'real_sd':round(statistics.stdev(r),2) if len(r)>1 else 0}
open('$RESULTS','a').write(json.dumps(o)+'\n')
print(f\"  => greedy {o['greedy_avg']:.2f}  real {o['real_avg']:.2f}  (VRAM {$vram} MiB)\")
"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
}

log "35B MTP production-faithful confirmation (b9381, q8 KV, ub4096, ctx131072, prod samplers)"
run_cell "base"     "$BASE_35B"
run_cell "MTP-n2"   "$MTP_35B" --spec-type draft-mtp --spec-draft-n-max 2
run_cell "MTP-n3"   "$MTP_35B" --spec-type draft-mtp --spec-draft-n-max 3
run_cell "MTP-n4"   "$MTP_35B" --spec-type draft-mtp --spec-draft-n-max 4

log "========================================"
python3 -c "
import json
rows=[json.loads(l) for l in open('$RESULTS')]
base_g=next((r['greedy_avg'] for r in rows if r.get('label')=='base'),0)
base_r=next((r['real_avg'] for r in rows if r.get('label')=='base'),0)
print('| Config | greedy TG | Δ | real TG | Δ | VRAM |')
print('|--|--:|--:|--:|--:|--:|')
for r in rows:
    if r.get('status')=='DIED': print(f\"| {r['label']} | DIED | | | | |\"); continue
    dg=f\"{(r['greedy_avg']-base_g)/base_g*100:+.1f}%\" if base_g else ''
    dr=f\"{(r['real_avg']-base_r)/base_r*100:+.1f}%\" if base_r else ''
    print(f\"| {r['label']} | {r['greedy_avg']:.2f} | {dg} | {r['real_avg']:.2f} | {dr} | {r['vram_mb']} |\")
"
log "Results in $RESULTS"

# restart production (b9186, unchanged)
log ""; log "Restarting production..."
stop_all
RADV_PERFTEST=rm_kq=1 nohup "$HOME/ai-runtime/llama-server/llama-server" \
    --model "$BASE_35B" --ctx-size 131072 --host 127.0.0.1 --port 8001 \
    --parallel 1 --jinja -ngl 99 -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    -fa 1 --cache-type-k q8_0 --cache-type-v q8_0 \
    --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
    --presence-penalty 1.5 --reasoning-budget 8192 \
    -b 4096 -ub 4096 --threads 6 \
    --chat-template-kwargs '{"preserve_thinking": true}' \
    > /tmp/llama-server-restart.log 2>&1 &
log "Production restarting (PID $!). Done."
