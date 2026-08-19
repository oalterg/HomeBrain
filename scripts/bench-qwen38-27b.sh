#!/usr/bin/env bash
# Qwen3.8-27B UD-IQ4_XS sweep on the RX 9060 XT (16 GB) box.
#
# Arch is `qwen35` — the same Gated-DeltaNet hybrid as Qwen3.6-27B (64 layers,
# 16 full-attention + 48 DeltaNet), so the DeltaNet-safe constraints carry over:
# no -ot (partial expert offload defeats the fused kernel), -ngl 99, and
# -fa + --cache-type-k/v are mandatory on AMD Vulkan.
#
# The Dynamic V3 file is 13,592 MiB against 16,304 MiB usable (the IQ4_XS it
# replaced was 14,978 MiB). The only levers left are
# ctx, KV quant, and the micro-batch that sizes the compute buffer.
#
# Usage: bench-qwen38-27b.sh <phase> [args...]
#   fit   <kv> <ub> <ctx...>   — load + shallow TG + PP@2k per ctx
#   tune  <ctx> <kv> <b/ub pairs...>
#   evict <label> <ctx> <kv> <b> <ub>  — deep-fill eviction protocol
set +e

BIN="${LLAMA_BIN:-$HOME/ai-runtime/llama-server/llama-server}"
LIBDIR="$(dirname "$BIN")"
MD="$HOME/models"
BD="$HOME/bench-upgrade"
PORT=8099
MODEL="$MD/Qwen3.8-27B-UD-IQ4_XS.gguf"
VP=/sys/class/drm/card1/device/mem_info_vram_used
TOTAL=16304
RESULTS="$BD/qwen38-27b.jsonl"

log() { echo "[$(date +%H:%M:%S)] $*"; }
vram() { echo $(( $(cat "$VP") / 1048576 )); }
hr() { echo $(( TOTAL - $(vram) )); }

mkdir -p "$BD"

# Thinking-mode samplers from the Qwen3.8 model card.
SAMPLERS=(--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0)

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { exec 3<&- 2>/dev/null; return 0; }; return 1; }

# The previous server must be GONE — process reaped, port released, VRAM drained —
# before the next one binds. Draining on VRAM alone let a dying server keep :8099
# and answer /health for the next cell, which reported bogus 0 t/s failures.
stop_all() {
    pkill -x llama-server 2>/dev/null
    local i=0
    while pgrep -x llama-server >/dev/null && [ "$i" -lt 30 ]; do sleep 1; i=$((i+1)); done
    if pgrep -x llama-server >/dev/null; then pkill -9 -x llama-server 2>/dev/null; sleep 3; fi
    i=0
    while port_busy && [ "$i" -lt 30 ]; do sleep 1; i=$((i+1)); done
    i=0
    while [ "$(vram)" -gt 800 ] && [ "$i" -lt 30 ]; do sleep 1; i=$((i+1)); done
}

# Greedy shallow probe: 200 tokens, no prompt cache, deterministic.
cat > "$BD/q38_shallow.json" <<'PY'
{"prompt":"<|im_start|>user\nList 40 tips for writing clean Python code.<|im_end|>\n<|im_start|>assistant\n","n_predict":200,"temperature":0.0,"top_k":1,"cache_prompt":false}
PY

# PP probe at a fixed ~2.4k depth so PP numbers stay comparable across cells.
python3 - > "$BD/q38_pp.json" <<'PY'
import json
para = ("In a distributed inference system the trade-off between throughput and latency "
        "is governed by batch size, KV-cache layout, expert routing, and memory residency. ")
print(json.dumps({"prompt": para * 75, "n_predict": 8, "temperature": 0.0, "cache_prompt": False}))
PY

# /health flips to OK before the server can actually serve, so readiness is
# gated on a real 1-token completion. Without this the harness measured
# half-allocated servers and reported them as 0 t/s failures.
wait_healthy() {
    local pid="$1" i=0
    while [ "$i" -lt 300 ]; do
        kill -0 "$pid" 2>/dev/null || return 2
        if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            if curl -sf --max-time 120 -H 'Content-Type: application/json' \
                 -d '{"prompt":"hi","n_predict":1,"temperature":0.0,"cache_prompt":false}' \
                 "http://127.0.0.1:$PORT/completion" 2>/dev/null | grep -q '"timings"'; then
                echo "$i"; return 0
            fi
        fi
        sleep 1; i=$((i+1))
    done
    return 3
}

tg_once() {
    curl -sf --max-time 300 -H 'Content-Type: application/json' -d @"$BD/q38_shallow.json" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null \
        | python3 -c "import sys,json;print(round(json.load(sys.stdin).get('timings',{}).get('predicted_per_second',0),2))" 2>/dev/null || echo 0
}

# A zero is only believable if the server is actually gone; otherwise retry once.
tg() {
    local v; v=$(tg_once)
    if [ "$v" = "0" ] && port_busy; then sleep 5; v=$(tg_once); fi
    echo "$v"
}

pp() {
    curl -sf --max-time 300 -H 'Content-Type: application/json' -d @"$BD/q38_pp.json" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null \
        | python3 -c "import sys,json;t=json.load(sys.stdin).get('timings',{});print(int(t.get('prompt_n',0)),round(t.get('prompt_per_second',0),1))" 2>/dev/null || echo "0 0"
}

# Build a deep prompt of ~$1 tokens using the server's own tokenizer to size it.
make_deep() {
    local target="$1"
    local para="In a distributed inference system the trade-off between throughput and latency is governed by batch size, KV-cache layout, expert routing, and memory residency. "
    local per_rep
    per_rep=$(curl -sf --max-time 60 -H 'Content-Type: application/json' \
        -d "$(python3 -c "import json;print(json.dumps({'content':'$para'}))")" \
        "http://127.0.0.1:$PORT/tokenize" 2>/dev/null \
        | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('tokens',[])))" 2>/dev/null)
    [ -z "$per_rep" ] || [ "$per_rep" -lt 1 ] 2>/dev/null && per_rep=30
    python3 - "$target" "$per_rep" > "$BD/q38_deep.json" <<'PY'
import json, sys
target, per_rep = int(sys.argv[1]), int(sys.argv[2])
para = ("In a distributed inference system the trade-off between throughput and latency "
        "is governed by batch size, KV-cache layout, expert routing, and memory residency. ")
print(json.dumps({"prompt": para * max(1, target // per_rep),
                  "n_predict": 8, "temperature": 0.0, "cache_prompt": True}))
PY
}

deep() {
    curl -sf --max-time 1800 -H 'Content-Type: application/json' -d @"$BD/q38_deep.json" \
        "http://127.0.0.1:$PORT/completion" 2>/dev/null \
        | python3 -c "import sys,json;t=json.load(sys.stdin).get('timings',{});print(int(t.get('prompt_n',0)),round(t.get('prompt_per_second',0),1))" 2>/dev/null || echo "0 0"
}

start_server() {
    local ctx="$1" kv="$2" b="$3" ub="$4"; shift 4
    # EXTRA lets the fit/tune phases carry flags they have no positional slot
    # for (e.g. --spec-type draft-mtp).
    local extra=(); [ -n "$EXTRA" ] && read -r -a extra <<< "$EXTRA"
    RADV_PERFTEST=rm_kq=1 LD_LIBRARY_PATH="$LIBDIR" "$BIN" \
        --model "$MODEL" --ctx-size "$ctx" --host 127.0.0.1 --port "$PORT" \
        --parallel 1 --jinja -ngl 99 -fa on \
        --cache-type-k "$kv" --cache-type-v "$kv" \
        "${SAMPLERS[@]}" -b "$b" -ub "$ub" --threads 6 "${extra[@]}" "$@" \
        > "$BD/q38.log" 2>&1 &
    echo $!
}

# ---------------------------------------------------------------- fit phase
phase_fit() {
    local kv="$1" ub="$2"; shift 2
    local b=$(( ub < 2048 ? 2048 : ub ))
    for ctx in "$@"; do
        stop_all
        local label="fit-ctx${ctx}-${kv}-ub${ub}"
        log "===== $label ====="
        local pid secs
        pid=$(start_server "$ctx" "$kv" "$b" "$ub")
        secs=$(wait_healthy "$pid"); local rc=$?
        if [ "$rc" -ne 0 ]; then
            log "  FAILED TO LOAD (rc=$rc)"
            tail -5 "$BD/q38.log" | sed 's/^/    /'
            python3 -c "
import json; open('$RESULTS','a').write(json.dumps({'phase':'fit','label':'$label','ctx':$ctx,'kv':'$kv','ub':$ub,'status':'FAILED'})+'\n')"
            kill "$pid" 2>/dev/null
            continue
        fi
        local idle=$(vram)
        log "  healthy in ${secs}s | VRAM ${idle} MiB | headroom $((TOTAL-idle)) MiB"
        curl -sf --max-time 300 -d @"$BD/q38_shallow.json" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
        local t1 t2; t1=$(tg); t2=$(tg)
        local pn ps; read pn ps <<<"$(pp)"
        local after=$(vram)
        log "  TG ${t1} / ${t2} t/s | PP@${pn} ${ps} t/s | VRAM after ${after} (hr $((TOTAL-after)))"
        python3 -c "
import json
open('$RESULTS','a').write(json.dumps({'phase':'fit','label':'$label','ctx':$ctx,'kv':'$kv','ub':$ub,
  'status':'OK','load_s':$secs,'vram_idle':$idle,'hr_idle':$((TOTAL-idle)),
  'tg1':$t1,'tg2':$t2,'pp_n':$pn,'pp_ts':$ps,'vram_after':$after})+'\n')"
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    done
    stop_all
}

# ---------------------------------------------------------------- tune phase
phase_tune() {
    local ctx="$1" kv="$2"; shift 2
    for pair in "$@"; do
        local b="${pair%%/*}" ub="${pair##*/}"
        stop_all
        local label="tune-ctx${ctx}-${kv}-b${b}-ub${ub}"
        log "===== $label ====="
        local pid secs
        pid=$(start_server "$ctx" "$kv" "$b" "$ub")
        secs=$(wait_healthy "$pid"); local rc=$?
        if [ "$rc" -ne 0 ]; then
            log "  FAILED (rc=$rc)"; tail -5 "$BD/q38.log" | sed 's/^/    /'; kill "$pid" 2>/dev/null; continue
        fi
        local idle=$(vram)
        curl -sf --max-time 300 -d @"$BD/q38_shallow.json" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
        local t1 t2 t3; t1=$(tg); t2=$(tg); t3=$(tg)
        local pn ps; read pn ps <<<"$(pp)"
        log "  VRAM ${idle} (hr $((TOTAL-idle))) | TG ${t1}/${t2}/${t3} | PP@${pn} ${ps}"
        python3 -c "
import json, statistics
v=[x for x in [$t1,$t2,$t3] if x>0]
open('$RESULTS','a').write(json.dumps({'phase':'tune','label':'$label','ctx':$ctx,'kv':'$kv','b':$b,'ub':$ub,
  'vram_idle':$idle,'hr_idle':$((TOTAL-idle)),'tg_avg':round(statistics.mean(v),2) if v else 0,
  'tg_runs':[$t1,$t2,$t3],'pp_n':$pn,'pp_ts':$ps})+'\n')"
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    done
    stop_all
}

# --------------------------------------------------------------- evict phase
# Protocol: fresh shallow TG -> fill ~92% of ctx -> shallow TG -> fill again ->
# shallow TG. Eviction = VRAM drops and stays down while shallow TG collapses.
phase_evict() {
    local label="$1" ctx="$2" kv="$3" b="$4" ub="$5"; shift 5
    stop_all
    log "===== EVICT $label | ctx=$ctx kv=$kv b=$b ub=$ub $* ====="
    local pid secs
    pid=$(start_server "$ctx" "$kv" "$b" "$ub" "$@")
    secs=$(wait_healthy "$pid"); local rc=$?
    if [ "$rc" -ne 0 ]; then
        log "  FAILED (rc=$rc)"; tail -8 "$BD/q38.log" | sed 's/^/    /'; kill "$pid" 2>/dev/null; return 1
    fi
    local idle=$(vram)
    log "  healthy ${secs}s | idle VRAM ${idle} | headroom $((TOTAL-idle))"
    curl -sf --max-time 300 -d @"$BD/q38_shallow.json" "http://127.0.0.1:$PORT/completion" >/dev/null 2>&1
    local t0=$(tg); local v0=$(vram)
    log "  shallow TG (fresh):      ${t0} t/s | VRAM ${v0} (hr $((TOTAL-v0)))"
    make_deep $(( ctx * 92 / 100 ))
    local dn1 dp1; read dn1 dp1 <<<"$(deep)"
    local vd1=$(vram)
    log "  deep fill #1: ${dn1} tok @ ${dp1} t/s | VRAM ${vd1} (hr $((TOTAL-vd1)))"
    local t1=$(tg); local v1=$(vram)
    log "  shallow TG (post-deep1): ${t1} t/s | VRAM ${v1} (hr $((TOTAL-v1)))"
    local dn2 dp2; read dn2 dp2 <<<"$(deep)"
    local vd2=$(vram)
    log "  deep fill #2: ${dn2} tok @ ${dp2} t/s | VRAM ${vd2} (hr $((TOTAL-vd2)))"
    local t2=$(tg); local v2=$(vram)
    log "  shallow TG (post-deep2): ${t2} t/s | VRAM ${v2} (hr $((TOTAL-v2)))"
    python3 -c "
import json
t0,t1,t2=$t0,$t1,$t2
ok = t0>0 and t2 >= 0.90*t0 and t1 >= 0.90*t0
verdict = 'PASS' if ok else ('DEGRADED/EVICTS' if t0>0 else 'ERROR')
o={'phase':'evict','label':'$label','ctx':$ctx,'kv':'$kv','b':$b,'ub':$ub,
   'vram_idle':$idle,'hr_idle':$((TOTAL-idle)),
   'tg_fresh':t0,'tg_post1':t1,'tg_post2':t2,
   'deep1_n':$dn1,'deep1_pp':$dp1,'deep2_n':$dn2,'deep2_pp':$dp2,
   'vram_deep1':$vd1,'vram_deep2':$vd2,'vram_final':$v2,'verdict':verdict}
open('$RESULTS','a').write(json.dumps(o)+'\n')
print(f'  >>> $label: fresh {t0} -> {t1} -> {t2} t/s  ({100*(t2-t0)/t0:+.1f}%)  {verdict}')"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    stop_all
}

case "$1" in
    fit)   shift; phase_fit "$@" ;;
    tune)  shift; phase_tune "$@" ;;
    evict) shift; phase_evict "$@" ;;
    *) echo "usage: $0 {fit|tune|evict} ..."; exit 1 ;;
esac
log "DONE — results in $RESULTS"
