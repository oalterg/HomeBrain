#!/usr/bin/env bash
#
# Unit tests for detect_platform() in common.sh — the single probe that decides
# which llama.cpp binary, model profile, telemetry reader and host hardening a
# box gets. It reads sysfs through $HB_SYSFS_ROOT and resolves `lspci`/`uname`
# from $PATH, so every case here runs against a fixture tree with no GPU, no
# root and no network.
#
#   bash scripts/tests/test_platform_detect.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- Fixture builders ---------------------------------------------------------

# fixture <name> [driver...] -> echoes a sysfs root. Each driver builds the
# render-node symlink real sysfs exposes, numbered from renderD128 upward:
# renderD128/device/driver -> bus/pci/drivers/<name>. Multiple drivers model a
# hybrid box, where node order is not the same as card preference.
fixture() {
    local name="$1"; shift
    local root="$TMP/$name" node=128 driver
    mkdir -p "$root/sys/class/drm"
    for driver in "$@"; do
        mkdir -p "$root/sys/bus/pci/drivers/$driver"
        mkdir -p "$root/sys/class/drm/renderD${node}/device"
        ln -sf "../../../bus/pci/drivers/$driver" "$root/sys/class/drm/renderD${node}/device/driver"
        node=$((node + 1))
    done
    echo "$root"
}

# stub_path <arch> [lspci-output] -> echoes a self-contained PATH dir.
#
# This dir becomes the *entire* PATH, so it has to carry every external
# detect_platform needs. That is the point: omitting the lspci-output argument
# has to mean lspci is genuinely unreachable. An earlier version left /usr/bin
# on the PATH and the real lspci leaked in — which passed on a macOS dev box
# (no lspci exists there) and failed on the Linux target with a GPU in it.
stub_path() {
    local arch="$1" lspci_out="${2-__ABSENT__}"
    local dir="$TMP/bin-$arch-${RANDOM}"
    mkdir -p "$dir"

    # Everything sourcing common.sh and running detect_platform shells out to.
    local tool src
    for tool in basename readlink mkdir cat id dirname grep sed tr; do
        src="$(command -v "$tool" 2>/dev/null)" && ln -sf "$src" "$dir/$tool"
    done

    cat > "$dir/uname" <<EOF
#!/bin/sh
[ "\$1" = "-m" ] && echo "$arch" && exit 0
exec $(command -v uname) "\$@"
EOF
    chmod +x "$dir/uname"

    if [[ "$lspci_out" != "__ABSENT__" ]]; then
        printf '#!/bin/sh\ncat <<"XEOF"\n%s\nXEOF\n' "$lspci_out" > "$dir/lspci"
        chmod +x "$dir/lspci"
    fi
    echo "$dir"
}

# probe <sysfs-root> <stub-bin-dir> -> echoes "arch driver backend memory tag has_gpu"
# Runs in a subshell so each case starts from a clean environment. PATH becomes
# *only* the stub dir, so a case asserting "no lspci" really has none — see the
# note in stub_path about why a prefix is not enough.
probe() {
    local root="$1" bindir="$2"
    (
        export HB_SYSFS_ROOT="$root"
        export PATH="$bindir"
        # shellcheck source=../common.sh disable=SC1091
        source "$COMMON" >/dev/null 2>&1
        echo "$HB_ARCH $HB_GPU_DRIVER $HB_GPU_BACKEND $HB_GPU_MEMORY $HB_PLATFORM_TAG $HAS_GPU"
    )
}

# case <label> <expected> <sysfs-root> <stub-bin-dir>
case_is() {
    local label="$1" want="$2" got
    got="$(probe "$3" "$4")"
    if [[ "$got" == "$want" ]]; then ok "$label"; else bad "$label
          want: $want
          got:  $got"; fi
}

# --- Cases --------------------------------------------------------------------

echo "== render-node driver detection =="

# The production box today. This is the regression that matters most: the record
# must keep resolving exactly as it did before the platform record existed.
case_is "x86_64 + amdgpu -> vulkan/discrete" \
    "x86_64 amdgpu vulkan discrete x86_64-vulkan true" \
    "$(fixture amd amdgpu)" "$(stub_path x86_64)"

# DGX Spark class. Unified memory is what suppresses the VRAM watermark check.
case_is "aarch64 + nvidia -> cuda/unified" \
    "aarch64 nvidia cuda unified aarch64-cuda true" \
    "$(fixture nv nvidia)" "$(stub_path aarch64)"

case_is "x86_64 + nvidia -> cuda/discrete" \
    "x86_64 nvidia cuda discrete x86_64-cuda true" \
    "$(fixture nvx nvidia)" "$(stub_path x86_64)"

case_is "x86_64 + i915 -> vulkan/unified (iGPU shares system RAM)" \
    "x86_64 i915 vulkan unified x86_64-vulkan true" \
    "$(fixture intel i915)" "$(stub_path x86_64)"

echo "== hybrid boxes (node order != card preference) =="

# The iGPU takes renderD128 and would win a naive first-match scan. It is not
# the card to size the model for, nor the one whose backend we install.
case_is "i915 on renderD128 + amdgpu on renderD129 -> amdgpu" \
    "x86_64 amdgpu vulkan discrete x86_64-vulkan true" \
    "$(fixture hybrid_amd i915 amdgpu)" "$(stub_path x86_64)"

case_is "i915 on renderD128 + nvidia on renderD129 -> nvidia" \
    "x86_64 nvidia cuda discrete x86_64-cuda true" \
    "$(fixture hybrid_nv i915 nvidia)" "$(stub_path x86_64)"

echo "== no-GPU targets =="

# HomeCloud on an RPi5. v3d exposes a render node but cannot run inference, so
# it must not be mistaken for a compute GPU — this is the check that keeps the
# AI stack off the production RPi boxes.
case_is "aarch64 + v3d (RPi VideoCore) -> none" \
    "aarch64 none none none aarch64-none false" \
    "$(fixture rpi v3d)" "$(stub_path aarch64)"

case_is "no render node, no lspci -> none" \
    "x86_64 none none none x86_64-none false" \
    "$(fixture bare)" "$(stub_path x86_64)"

echo "== lspci fallback (driver present on the bus but not probed) =="

# The Navi 44 VCN ring-test bug takes amdgpu down and removes the render node.
# The modprobe workaround that fixes it is applied under HAS_GPU, so this path
# has to keep reporting amdgpu or the box cannot self-heal.
case_is "no render node + AMD on the bus -> amdgpu" \
    "x86_64 amdgpu vulkan discrete x86_64-vulkan true" \
    "$(fixture amd_unprobed)" \
    "$(stub_path x86_64 '01:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 44 [Radeon RX 9060 XT]')"

case_is "no render node + NVIDIA on the bus -> nvidia" \
    "x86_64 nvidia cuda discrete x86_64-cuda true" \
    "$(fixture nv_unprobed)" \
    "$(stub_path x86_64 '01:00.0 VGA compatible controller: NVIDIA Corporation GB202 [GeForce RTX 5090]')"

# A display chip from any other vendor must not flip a board into the AI stack.
# The old bare VGA|3D|Display match would have said "GPU" here.
case_is "no render node + non-compute VGA -> none" \
    "aarch64 none none none aarch64-none false" \
    "$(fixture other_vga)" \
    "$(stub_path aarch64 '00:01.0 VGA compatible controller: Red Hat, Inc. Virtio GPU')"

echo "== emit_platform_json =="

emit_root="$(fixture emit amdgpu)"
emit_bin="$(stub_path x86_64)"
out="$(
    export HB_SYSFS_ROOT="$emit_root"
    export PATH="${emit_bin}"
    # shellcheck source=../common.sh disable=SC1091
    source "$COMMON" >/dev/null 2>&1
    emit_platform_json -
)"
want='{"arch":"x86_64","gpu_driver":"amdgpu","gpu_backend":"vulkan","gpu_memory":"discrete","platform_tag":"x86_64-vulkan","has_gpu":true}'
if [[ "$(echo "$out" | tr -d '\n')" == "$want" ]]; then
    ok "emits the record as JSON on stdout"
else
    bad "emit_platform_json -
          want: $want
          got:  $out"
fi

if command -v python3 >/dev/null 2>&1 && echo "$out" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
    ok "record parses as JSON (app.py reads this)"
else
    bad "record is not valid JSON"
fi

echo "== model profile resolution =="

# Same jq expressions setup_llama_server uses, against the real config. The
# regression that matters: every committed model must resolve exactly as it did
# before profiles existed when read on the production platform tag.
MODELS="$SCRIPT_DIR/../../config/platform_models.json"
if ! command -v jq >/dev/null 2>&1; then
    bad "jq not available — cannot check profile resolution"
elif [[ ! -f "$MODELS" ]]; then
    bad "platform_models.json not found at $MODELS"
else
    resolve() {  # resolve <id> <tag> <field>
        jq -r --arg id "$1" --arg tag "$2" \
            ".models[] | select(.id == \$id) | (.profiles[\$tag] // {}).$3 // .$3 // \"\"" "$MODELS"
    }
    unprofiled() { jq -r --arg id "$1" ".models[] | select(.id == \$id) | .$2 // \"\"" "$MODELS"; }

    drift=0
    for id in $(jq -r '.models[].id' "$MODELS"); do
        for field in context_window extra_flags min_healthy_vram_mb; do
            if [[ "$(resolve "$id" x86_64-vulkan "$field")" != "$(unprofiled "$id" "$field")" ]]; then
                bad "x86_64-vulkan resolution drifted for ${id}.${field}"
                drift=1
            fi
        done
    done
    [[ "$drift" -eq 0 ]] && ok "all committed models resolve unchanged on x86_64-vulkan"

    # And the mechanism actually does something on a platform that declares one.
    ctx_x86="$(resolve Qwen3.6-35B-A3B-UD-Q5_K_XL x86_64-vulkan context_window)"
    ctx_arm="$(resolve Qwen3.6-35B-A3B-UD-Q5_K_XL aarch64-cuda context_window)"
    if [[ "$ctx_x86" == "81920" && "$ctx_arm" == "131072" ]]; then
        ok "aarch64-cuda profile overrides context_window (81920 -> 131072)"
    else
        bad "aarch64-cuda override: want 81920/131072, got ${ctx_x86}/${ctx_arm}"
    fi

    # The CPU-expert offload exists only because 16 GB cannot hold the model.
    if resolve Qwen3.6-35B-A3B-UD-Q5_K_XL aarch64-cuda extra_flags | grep -q '\-ot '; then
        bad "aarch64-cuda profile still carries the 16 GB -ot expert offload"
    else
        ok "aarch64-cuda profile drops the -ot expert offload"
    fi

    # A model with no profiles block must fall through to its own values.
    if [[ "$(resolve Qwen3.6-27B-IQ4_XS aarch64-cuda context_window)" == "32768" ]]; then
        ok "model without a profile falls back to its top-level values"
    else
        bad "unprofiled model did not fall back on an unknown tag"
    fi
fi

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
