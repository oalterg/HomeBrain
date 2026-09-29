#!/usr/bin/env bash
# Exercise installation with a partial NEO prefix, without root or downloads.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/scripts" "$TMP/config" "$TMP/systemd"
SCRIPT_DIR="$TMP/scripts"
prefix="$TMP/neo"
mkdir -p "$prefix/usr/local/lib" "$prefix/usr/lib/x86_64-linux-gnu"
touch "$prefix/usr/local/lib/libigc.so.2" "$prefix/usr/lib/x86_64-linux-gnu/libze_intel_gpu.so.1"
jq --arg prefix "$prefix" '.llama_cpp.source_build["x86_64-sycl"] |= (.neo_prefix=$prefix | .oneapi.icpx="/bin/sh")' \
    "$ROOT/config/versions.json" > "$TMP/config/versions.json"
extract() {
    awk -v fn="$1" '$0 == fn "() {" {inside=1} inside {print} inside && $0 == "}" {exit}' \
        "$ROOT/scripts/utilities.sh" | sed "s|/etc/systemd/system|$TMP/systemd|g"
}
eval "$(extract _install_sycl_runtime)"
eval "$(extract refresh_whisper_runtime)"
eval "$(extract generate_whisper_services)"
HB_GPU_BACKEND=sycl
get_llama_bin_path() { echo "$TMP/llama-server"; }
log_info() { :; }
die() { echo "$*" >&2; exit 1; }
systemctl() {
    echo "$*" >> "$TMP/systemctl"
    return 0
}
curl() { echo "$*" >> "$TMP/downloads"; }
dpkg-deb() {
    case "$2" in
        *intel-opencl-icd_*)
            mkdir -p "$3/usr/lib/x86_64-linux-gnu/intel-opencl"
            touch "$3/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so" ;;
    esac
}
_install_sycl_runtime
test -f "$prefix/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so"
grep -q 'intel-opencl-icd_' "$TMP/downloads"
echo 'ok: existing Level Zero installation receives missing OpenCL runtime'
: > "$TMP/downloads"
_install_sycl_runtime
test ! -s "$TMP/downloads"
echo 'ok: complete runtime does not download again'

touch "$TMP/systemd/whisper-server.service"
: > "$TMP/systemctl"
refresh_whisper_runtime
grep -q 'GGML_DISABLE_VULKAN=1' "$TMP/systemd/whisper-server.service.d/10-cpu-only.conf"
grep -q '^restart whisper-server$' "$TMP/systemctl"
grep -q '^start whisper-proxy$' "$TMP/systemctl"
echo 'ok: existing Whisper installation isolates Vulkan and restores the proxy'
: > "$TMP/systemctl"
refresh_whisper_runtime
test ! -s "$TMP/systemctl"
echo 'ok: repeated refresh does not restart voice services'

HOMEBRAIN_HOME="$TMP/home"
HOMEBRAIN_USER=test
mkdir -p "$HOMEBRAIN_HOME/ai-runtime/whisper-proxy"
cp "$ROOT/scripts/whisper_proxy.py" "$SCRIPT_DIR/whisper_proxy.py"
chown() { :; }
generate_whisper_services /tmp/whisper-server /tmp/model.bin
grep -q 'GGML_DISABLE_VULKAN=1' "$TMP/systemd/whisper-server.service"
grep -q -- '--no-gpu' "$TMP/systemd/whisper-server.service"
echo 'ok: fresh Whisper service disables Vulkan discovery and GPU inference'
