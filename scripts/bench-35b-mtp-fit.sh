#!/usr/bin/env bash
# Find a DEPLOYABLE 35B MTP config at ctx=131072 on b9381 (q8+ub4096 OOMs).
# Tries q8/ub2048 (best numerics) and q4/ub2048 (known to fit). Greedy + real (temp=1.0) TG.
set +e
BENCH_DIR="$HOME/bench-upgrade"; MODEL_DIR="$HOME/models"; PORT=8099
RESULTS="$BENCH_DIR/mtp35b-fit.jsonl"; BIN="$BENCH_DIR/b9381/llama-b9381"
BASE_35B="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf"
MTP_35B="$MODEL_DIR/Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL.gguf"
VRAM_PATH=/sys/class/drm/card1/device/mem_info_vram_used
[ -f "$VRAM_PATH" ] || VRAM_PATH=/sys/class/drm/card0/device/mem_info_vram_used
log() { echo "[$(date +%H:%M:%S)] $*"; }
vram_mb() { echo $(( $(cat "$VRAM_PATH") / 1048576 )); }
mkdir -p "$BENCH_DIR"; > "$RESULTS"
stop_all() { pkill -x llama-server 2>/dev/null||true; sleep 3; pkill -9 -x llama-server 2>/dev/null||true; sleep 2; local i=0; while [ "$(vram_mb)" -gt 500 ]&&[ "$i" -lt 25 ]; do sleep 1; i=$((i+1)); done; }

GREEDY="$BENCH_DIR/probe_greedy35.json"; REAL="$BENCH_DIR/probe_real35.json"
[ -f "$GREEDY" ] || python3 -c "import json;print(json.dumps({'prompt':'<|im_start|>user\nList the integers from 1 to 200 separated by commas.<|im_end|>\n<|im_start|>assistant\n','n_predict':512,'temperature':0.0,'top_p':1.0,'cache_prompt':False}))" > "$GREEDY"
[ -f "$REAL" ] || python3 -c "import json;print(json.dumps({'prompt':'<|im_start|>user\nWrite a detailed Python module that implements a thermostat controller with hysteresis, scheduling, and a small test suite. Include docstrings.<|im_end|>\n<|im_start|>assistant\n','n_predict':512,'cache_prompt':False}))" > "$REAL"

measure() { local resp; resp=$(curl -sf --max-time 240 -H "Content-Type: application/json" -d @"$1" "http://127.0.0.1:$PORT/completion" 2>/dev/null); [ -z "$resp" ]&&{ echo "0 0"; return 1; }; python3 -c "
import sys,json; r=json.loads(sys.stdin.read()); t=r.get('timings',{}); print(f\"{t.get('predicted_per_second',0)} {t.get('predicted_n',0)}\")" <<< "$resp" 2>/dev/null||echo "0 0"; }

run_cell() {
    local label="$1" model="$2" kv="$3" ub="$4"; shift 4; local spec=("$@")
    stop_all; log "--- $label | kv=$kv ub=$ub ${spec[*]} ---"
    RADV_PERFTEST=rm_kq=1 "$BIN/llama-server" --model "$model" --ctx-size 131072 \
        --host 127.0.0.1 --port "$PORT" --parallel 1 --jinja -ngl 99 \
        -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" -fa on \
        --cache-type-k "$kv" --cache-type-v "$kv" \
        --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 1.5 \
        --reasoning-budget 8192 -b 4096 -ub "$ub" --threads 6 \
        --chat-template-kwargs '{"preserve_thinking": true}' "${spec[@]}" \
        > "$BENCH_DIR/server.log" 2>&1 &
    local pid=$! i=0
    while [ "$i" -lt 240 ]; do
        kill -0 "$pid" 2>/dev/null || { log "  DIED (likely OOM)"; grep -iE "DeviceLost|alloc|out of|error" "$BENCH_DIR/server.log"|tail -2; \
            python3 -c "open('$RESULTS','a').write('{\"label\":\"$label\",\"kv\":\"$kv\",\"ub\":$ub,\"status\":\"DIED\"}\n')"; return 1; }
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 1; i=$((i+1))
    done
    [ "$i" -ge 240 ]&&{ log "  TIMEOUT"; kill "$pid" 2>/dev/null; return 1; }
    local vram=$(vram_mb); log "  Healthy (${i}s, VRAM ${vram} MiB, headroom $((16304-vram)))"
    curl -sf --max-time 240 -d @"$GREEDY" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    local g_vals="" r_vals=""
    for run in 1 2 3; do
        read g _ <<< "$(measure "$GREEDY")"; read r _ <<< "$(measure "$REAL")"
        log "    run $run: greedy=$g real=$r"; g_vals="$g_vals $g"; r_vals="$r_vals $r"
    done
    python3 -c "
import json,statistics
g=[float(x) for x in '$g_vals'.split() if float(x)>0]; r=[float(x) for x in '$r_vals'.split() if float(x)>0]
o={'label':'$label','kv':'$kv','ub':$ub,'vram_mb':$vram,
   'greedy_avg':round(statistics.mean(g),2) if g else 0,'greedy_sd':round(statistics.stdev(g),2) if len(g)>1 else 0,
   'real_avg':round(statistics.mean(r),2) if r else 0,'real_sd':round(statistics.stdev(r),2) if len(r)>1 else 0}
open('$RESULTS','a').write(json.dumps(o)+'\n')
print(f\"  => greedy {o['greedy_avg']:.2f} real {o['real_avg']:.2f} (VRAM {$vram})\")"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
}

log "35B MTP deployable-config search (b9381, ctx131072, prod samplers)"
# q8 KV at ub2048 — does freeing the compute buffer let MTP fit with best numerics?
run_cell "base-q8-ub2048"  "$BASE_35B" q8_0 2048
run_cell "MTPn2-q8-ub2048" "$MTP_35B"  q8_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
# q4 KV at ub2048 — known to fit; confirm real gain + n3
run_cell "base-q4-ub2048"  "$BASE_35B" q4_0 2048
run_cell "MTPn2-q4-ub2048" "$MTP_35B"  q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 2
run_cell "MTPn3-q4-ub2048" "$MTP_35B"  q4_0 2048 --spec-type draft-mtp --spec-draft-n-max 3

log "========================================"
python3 -c "
import json
rows=[json.loads(l) for l in open('$RESULTS')]
print('| Config | greedy TG | real TG | VRAM | status |')
print('|--|--:|--:|--:|--|')
for r in rows:
    if r.get('status')=='DIED': print(f\"| {r['label']} | — | — | — | OOM |\"); continue
    print(f\"| {r['label']} | {r['greedy_avg']:.2f} | {r['real_avg']:.2f} | {r['vram_mb']} | ok |\")
"
log "Results in $RESULTS"
log ""; log "Restarting production..."; stop_all
RADV_PERFTEST=rm_kq=1 nohup "$HOME/ai-runtime/llama-server/llama-server" \
    --model "$BASE_35B" --ctx-size 131072 --host 127.0.0.1 --port 8001 \
    --parallel 1 --jinja -ngl 99 -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    -fa 1 --cache-type-k q8_0 --cache-type-v q8_0 --temp 1.0 --top-p 0.95 --top-k 20 \
    --min-p 0.0 --presence-penalty 1.5 --reasoning-budget 8192 -b 4096 -ub 4096 --threads 6 \
    --chat-template-kwargs '{"preserve_thinking": true}' > /tmp/llama-server-restart.log 2>&1 &
log "Production restarting (PID $!). Done."
