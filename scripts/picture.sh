#!/bin/bash
# One Krea 2 picture on a discrete Arc. Stops chat, runs the frozen workflow,
# and always starts chat again. The dashboard owns the request file; this
# script never takes a prompt on the command line.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/src/picture.py"
HOMEBRAIN_HOME="${HOMEBRAIN_HOME:-/home/homebrain}"
COMFY_HOME="${HB_COMFY_ROOT:-${HOMEBRAIN_HOME}/ComfyUI}"
COMFY_REV="79be670e2d9be63e238785af307369d2b9039ed1"
LAUNCHER="${HOMEBRAIN_HOME}/comfy-xpu.sh"
# homebrain has to be able to create this. /var/log/homebrain is root-only.
COMFY_LOG="${HOMEBRAIN_HOME}/picture-comfy.log"

say() { echo "$(date -Is) $*"; }

need_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "picture.sh must run as root" >&2
        exit 1
    fi
}

python() { command python3 "$PY" "$@"; }

llama_gone() {
    ! systemctl is-active --quiet llama-server \
        && ! pgrep -x llama-server >/dev/null
}

stop_llama() {
    systemctl stop llama-server || true
    local i
    for i in $(seq 1 60); do
        if llama_gone; then
            return 0
        fi
        sleep 1
    done
    say "llama-server did not stop"
    return 1
}

stop_comfy() {
    if pgrep -f "${COMFY_HOME}/.venv/bin/python main.py" >/dev/null 2>&1; then
        pkill -f "${COMFY_HOME}/.venv/bin/python main.py" || true
    fi
    local i
    for i in $(seq 1 30); do
        if ! pgrep -f "${COMFY_HOME}/.venv/bin/python main.py" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    pkill -9 -f "${COMFY_HOME}/.venv/bin/python main.py" || true
}

wait_comfy() {
    local i
    for i in $(seq 1 60); do
        if curl -sf --max-time 2 http://127.0.0.1:8188/system_stats >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_health() {
    local i
    for i in $(seq 1 90); do
        if curl -sf --max-time 2 http://127.0.0.1:8001/health >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

start_comfy() {
    mkdir -p "$(dirname "$COMFY_LOG")"
    # setsid so the server outlives this sudo. cd /tmp avoids reading
    # another user's uv.toml when the home directory is not searchable.
    sudo -u homebrain -H bash -c \
        "cd /tmp && setsid nohup ${LAUNCHER} >>$(printf '%q' "$COMFY_LOG") 2>&1 </dev/null &"
    wait_comfy
}

finished=0
chat_down=0
cleanup() {
    [[ "$finished" -eq 1 ]] && return
    finished=1
    python settle || true
    stop_comfy
    systemctl start llama-server || true
    if ! wait_health; then
        chat_down=1
        python mark-chat down || true
        say "chat did not come back"
    else
        say "chat restored"
    fi
}

apply_if_missing() {
    local marker="$1" file="$2" patchfile="$3" dir="$4"
    if [[ -f "$file" ]] && grep -q "$marker" "$file"; then
        say "already patched $(basename "$file")"
        return 0
    fi
    patch -d "$dir" -p1 --forward --batch < "$patchfile"
}

install_comfy() {
    if [[ ! -d /opt/intel/neo-26.35 ]]; then
        echo "neo 26.35 is not unpacked at /opt/intel/neo-26.35" >&2
        exit 1
    fi
    if [[ ! -d "$COMFY_HOME/.git" ]]; then
        local uv="${HOMEBRAIN_HOME}/.local/bin/uv"
        [[ -x "$uv" ]] || uv="$(command -v uv || true)"
        [[ -n "$uv" ]] || { echo "uv is required to install ComfyUI" >&2; exit 1; }
        sudo -u homebrain -H git clone https://github.com/Comfy-Org/ComfyUI.git "$COMFY_HOME"
        sudo -u homebrain -H git -C "$COMFY_HOME" fetch --depth 1 origin "$COMFY_REV"
        sudo -u homebrain -H git -C "$COMFY_HOME" checkout --detach "$COMFY_REV"
        sudo -u homebrain -H bash -c "cd ${HOMEBRAIN_HOME} && $(printf '%q' "$uv") python install 3.13"
        sudo -u homebrain -H bash -c "cd $(printf '%q' "$COMFY_HOME") && $(printf '%q' "$uv") venv --python 3.13 .venv"
        sudo -u homebrain -H bash -c "cd $(printf '%q' "$COMFY_HOME") && $(printf '%q' "$uv") pip install --python .venv/bin/python torch==2.14.0+xpu torchvision==0.29.0+xpu torchaudio==2.11.0+xpu --index-url https://download.pytorch.org/whl/xpu"
        sudo -u homebrain -H bash -c "cd $(printf '%q' "$COMFY_HOME") && $(printf '%q' "$uv") pip install --python .venv/bin/python -r requirements.txt"
    fi
    [[ -x "$COMFY_HOME/.venv/bin/python" ]] || { echo "ComfyUI venv is missing" >&2; exit 1; }

    apply_if_missing "_xpu_chunked_linear" \
        "$COMFY_HOME/comfy/ops.py" \
        "$ROOT/patches/comfyui-arc-b60.patch" \
        "$COMFY_HOME" || exit 1
    local int8
    int8="$(find "$COMFY_HOME/.venv" -path '*/comfy_kitchen/tensor/int8_utils.py' | head -1)"
    [[ -n "$int8" ]] || { echo "comfy-kitchen is not installed" >&2; exit 1; }
    apply_if_missing "_matmul_groups" \
        "$int8" \
        "$ROOT/patches/comfy-kitchen-int8.patch" \
        "$(dirname "$(dirname "$(dirname "$int8")")")" || exit 1
    if ! grep -q "flat1.cpu()" "$COMFY_HOME/comfy/weight_adapter/lora.py"; then
        echo "LoRA patch did not apply" >&2
        exit 1
    fi

    install -o homebrain -g homebrain -m 755 "$ROOT/config/comfy-xpu.sh" "$LAUNCHER"
    install -d -o homebrain -g homebrain -m 750 "${HOMEBRAIN_HOME}/pictures"
    install -d -m 755 /var/lib/homebrain
    install -m 644 "$ROOT/config/homebrain-picture.service" /etc/systemd/system/homebrain-picture.service
    systemctl daemon-reload
    # No [Install] section: this unit never starts at boot.
    say "picture runtime installed"
}

cmd_install() {
    need_root
    # common.sh is not nounset-safe. Restore it after the platform probe.
    set +u
    # shellcheck disable=SC1091
    source "$ROOT/scripts/common.sh"
    set -u
    if [[ "$HB_GPU_DRIVER" != "xe" || "$HB_GPU_MEMORY" != "discrete" || "$HB_GPU_BACKEND" != "sycl" ]]; then
        say "Picture button is only installed on a discrete Arc (${HB_GPU_DRIVER}/${HB_GPU_MEMORY})."
        exit 0
    fi
    install_comfy
    if ! python weights >/dev/null 2>&1; then
        say "Runtime is installed. Krea 2 weights are not all present yet, so the button stays hidden."
    fi
}

cmd_run() {
    need_root
    if ! python check; then
        python status error "The picture could not start."
        exit 1
    fi
    trap cleanup EXIT
    python status running "Chat is paused while the picture is made."
    if ! stop_llama; then
        python status error "Chat did not release the graphics card."
        exit 1
    fi
    # The chat model's file cache plus the picture weights do not both fit
    # in 30 GB. Drop the cache once llama has exited so Comfy can map its files.
    sync
    echo 3 > /proc/sys/vm/drop_caches || true
    stop_comfy
    if ! start_comfy; then
        python status error "The picture runtime did not start."
        exit 1
    fi
    python execute
    local rc=$?
    cleanup
    [[ "$chat_down" -eq 1 ]] && rc=1
    exit "$rc"
}

case "${1:-}" in
    install) cmd_install ;;
    run) cmd_run ;;
    *) echo "usage: picture.sh install|run" >&2; exit 2 ;;
esac
