#!/usr/bin/env bash
# Version A/B: b9186 (prod) vs b9381 (latest) on production-realistic configs.
# Measures TG (short prompt, 256 tok) and PP@2k (long prompt). Plus 35B MTP re-test on b9381.
set +e

BENCH_DIR="$HOME/bench-upgrade"
MODEL_DIR="$HOME/models"
PORT=8099
RESULTS="$BENCH_DIR/version-ab.jsonl"

BASE_35B="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf"
MTP_35B="$MODEL_DIR/Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL.gguf"
BASE_27B="$MODEL_DIR/Qwen3.6-27B-IQ4_XS.gguf"
MTP_27B="$MODEL_DIR/Qwen3.6-27B-MTP-IQ4_XS.gguf"

BIN_9186="$HOME/ai-runtime/llama-server"
BIN_9381="$BENCH_DIR/b9381/llama-b9381"

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

# --- Probe files ---
TG_PROBE="$BENCH_DIR/probe_predictable.json"   # short prompt, reused
PP_PROBE="$BENCH_DIR/probe_pp2k.json"
python3 -c "
import json
body = 'The home automation system manages lights, climate, security, and energy across many rooms. ' * 90
print(json.dumps({'prompt': '<|im_start|>user\n'+body+'\nSummarize.<|im_end|>\n<|im_start|>assistant\n',
                  'n_predict': 16, 'temperature': 0.0, 'top_p': 1.0, 'cache_prompt': False}))
" > "$PP_PROBE"

# measure: returns "tg_per_s pp_per_s prompt_n predicted_n"
measure() {
    local probe="$1" resp
    resp=$(curl -sf --max-time 240 -H "Content-Type: application/json" -d @"$probe" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null)
    [ -z "$resp" ] && { echo "0 0 0 0"; return 1; }
    python3 -c "
import sys, json
r = json.loads(sys.stdin.read()); t = r.get('timings', {})
print(f\"{t.get('predicted_per_second',0)} {t.get('prompt_per_second',0)} {t.get('prompt_n',0)} {t.get('predicted_n',0)}\")
" <<< "$resp" 2>/dev/null || echo "0 0 0 0"
}

run_cell() {
    local tag="$1" bindir="$2" label="$3" model="$4" ctx="$5" kv="$6" ub="$7"; shift 7
    local extra=("$@")
    stop_all
    log "--- [$tag] $label | ctx=$ctx kv=$kv ub=$ub ${extra[*]} ---"
    RADV_PERFTEST=rm_kq=1 "$bindir/llama-server" \
        --model "$model" --ctx-size "$ctx" --host 127.0.0.1 --port "$PORT" \
        --parallel 1 -ngl 99 -fa on --cache-type-k "$kv" --cache-type-v "$kv" \
        -t 6 -b 4096 -ub "$ub" "${extra[@]}" > "$BENCH_DIR/server.log" 2>&1 &
    local pid=$! i=0
    while [ "$i" -lt 240 ]; do
        kill -0 "$pid" 2>/dev/null || { log "  DIED"; tail -4 "$BENCH_DIR/server.log"; return 1; }
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 1; i=$((i+1))
    done
    [ "$i" -ge 240 ] && { log "  TIMEOUT"; kill "$pid" 2>/dev/null; return 1; }
    local vram=$(vram_mb)
    log "  Healthy (${i}s, VRAM ${vram} MiB)"
    # warmups
    curl -sf --max-time 240 -H "Content-Type: application/json" -d @"$TG_PROBE" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    curl -sf --max-time 240 -H "Content-Type: application/json" -d @"$PP_PROBE" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    local tg_vals="" pp_vals=""
    for run in 1 2 3; do
        read tg _ _ _ <<< "$(measure "$TG_PROBE")"
        read _ pp pn _ <<< "$(measure "$PP_PROBE")"
        log "    run $run: TG=$tg t/s  PP=$pp t/s (pn=$pn)"
        tg_vals="$tg_vals $tg"; pp_vals="$pp_vals $pp"
    done
    python3 -c "
import json, statistics
tg=[float(x) for x in '$tg_vals'.split() if float(x)>0]
pp=[float(x) for x in '$pp_vals'.split() if float(x)>0]
r={'tag':'$tag','label':'$label','ctx':$ctx,'kv':'$kv','ub':$ub,'vram_mb':$vram,
   'tg_avg':round(statistics.mean(tg),2) if tg else 0,
   'tg_stdev':round(statistics.stdev(tg),2) if len(tg)>1 else 0,
   'pp_avg':round(statistics.mean(pp),1) if pp else 0,
   'pp_stdev':round(statistics.stdev(pp),1) if len(pp)>1 else 0}
open('$RESULTS','a').write(json.dumps(r)+'\n')
print(f\"  => TG {r['tg_avg']:.2f}  PP {r['pp_avg']:.1f}  (VRAM {$vram} MiB)\")
"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
}

log "Version A/B + 35B MTP re-test"
log "============================="

# ===== Version A/B on production-realistic configs (no MTP) =====
for spec in "b9186:$BIN_9186" "b9381:$BIN_9381"; do
    tag="${spec%%:*}"; bin="${spec#*:}"
    log ""
    log "===== $tag — production configs ====="
    # 35B Q5_K_XL production config (ctx 131072, q8 KV, -ot 20-39, ub4096)
    run_cell "$tag" "$bin" "35B-Q5KXL-prod" "$BASE_35B" 131072 q8_0 4096 \
        -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU"
    # 27B IQ4_XS production config (ctx 49152, q4 KV, ub2048)
    run_cell "$tag" "$bin" "27B-IQ4XS-prod" "$BASE_27B" 49152 q4_0 2048
done

# ===== 35B MTP re-test on b9381 (does latest move the cliff / fix MoE MTP at high ctx?) =====
log ""
log "===== b9381 — 35B-A3B MTP across contexts ====="
for ctx in 32768 49152 131072; do
    run_cell "b9381" "$BIN_9381" "35B-base-c${ctx}" "$BASE_35B" "$ctx" q4_0 2048 \
        -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU"
    run_cell "b9381" "$BIN_9381" "35B-MTPn2-c${ctx}" "$MTP_35B" "$ctx" q4_0 2048 \
        -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" --spec-type draft-mtp --spec-draft-n-max 2
    run_cell "b9381" "$BIN_9381" "35B-MTPn3-c${ctx}" "$MTP_35B" "$ctx" q4_0 2048 \
        -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" --spec-type draft-mtp --spec-draft-n-max 3
done

# ===== 27B MTP re-test on b9381 (did the cliff move past 32768?) =====
log ""
log "===== b9381 — 27B MTP cliff probe ====="
for ctx in 32768 49152; do
    run_cell "b9381" "$BIN_9381" "27B-MTPn2-c${ctx}" "$MTP_27B" "$ctx" q4_0 2048 \
        --spec-type draft-mtp --spec-draft-n-max 2
done

log ""
log "========================================"
python3 -c "
import json
rows=[json.loads(l) for l in open('$RESULTS')]
print('| Tag | Label | Ctx | KV | TG (t/s) | PP (t/s) | VRAM |')
print('|--|--|--:|--|--:|--:|--:|')
for r in rows:
    print(f\"| {r['tag']} | {r['label']} | {r['ctx']} | {r['kv']} | {r['tg_avg']:.2f} ± {r['tg_stdev']:.2f} | {r['pp_avg']:.0f} ± {r['pp_stdev']:.0f} | {r['vram_mb']} |\")
"
log "Results in $RESULTS"

# ===== restart production =====
log ""; log "Restarting production..."
stop_all
RADV_PERFTEST=rm_kq=1 nohup "$BIN_9186/llama-server" \
    --model "$BASE_35B" --ctx-size 131072 --host 127.0.0.1 --port 8001 \
    --parallel 1 --jinja -ngl 99 -ot "blk.(2[0-9]|3[0-9]).ffn_.*exps=CPU" \
    -fa 1 --cache-type-k q8_0 --cache-type-v q8_0 \
    --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
    --presence-penalty 1.5 --reasoning-budget 8192 \
    -b 4096 -ub 4096 --threads 6 \
    --chat-template-kwargs '{"preserve_thinking": true}' \
    > /tmp/llama-server-restart.log 2>&1 &
log "Production restarting (PID $!). Done."
