#!/bin/bash

# --- Global Configuration ---
# Overridable so a probe can source this file without pointing at the live tree
# (emit_platform_json writes under INSTALL_DIR). Everything else keeps the
# hardcoded default it always had.
export INSTALL_DIR="${INSTALL_DIR:-/opt/homebrain}"
export LOG_DIR="/var/log/homebrain"
export ENV_FILE="$INSTALL_DIR/.env"
export COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
export OVERRIDE_FILE="$INSTALL_DIR/docker-compose.override.yml"
export BACKUP_MOUNTDIR="/mnt/backup"

# --- Canonical HomeBrain OS User ---
export HOMEBRAIN_USER="homebrain"
export HOMEBRAIN_HOME="/home/${HOMEBRAIN_USER}"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# --- Logging Helpers ---
log_info() { echo "[INFO] $1" >&2; }
log_warn() { echo "[WARN] $1" >&2; }
log_error() { echo "[ERROR] $1" >&2; }
die() { log_error "$1" >&2; exit 1; }

# --- Downgrade Protection ---
# HomeBrain's update path is one-way. Two things make a downgrade unsafe:
#   1. Nextcloud migrates its data + config version forward the first time a
#      newer image boots, then *refuses to start* on an older image
#      ("update needed" / "version of the data is higher than the docker
#      image version"). There is no supported automated rollback.
#   2. The Flask manager's templates and app.py move together; a partially- or
#      fully-applied downgrade leaves new templates rendered by an old app.py
#      that lacks the inject_platform() context processor, so every page 500s
#      with `'platform' is undefined`.
# The only safe way back is restore.sh from a pre-upgrade backup, so we refuse
# downgrades up front instead of letting them corrupt state.

# version_lt A B -> exit 0 (true) if version A is strictly older than B.
# Pure bash dot-segment compare: no `sort -V`, so it behaves identically on the
# Linux targets and on a macOS dev box running the test suite. Non-numeric
# trailing junk in a segment (e.g. "0-rc1") is stripped to its leading digits.
version_lt() {
    if [ "$1" = "$2" ]; then return 1; fi
    local IFS=.
    # shellcheck disable=SC2206  # word-splitting on '.' is intentional here
    local -a a=($1) b=($2)
    local i max=${#a[@]}
    if [ "${#b[@]}" -gt "$max" ]; then max=${#b[@]}; fi
    for ((i = 0; i < max; i++)); do
        local ai="${a[i]:-0}" bi="${b[i]:-0}"
        ai="${ai%%[!0-9]*}"; bi="${bi%%[!0-9]*}"
        ai=$((10#${ai:-0})); bi=$((10#${bi:-0}))
        if ((ai < bi)); then return 0; fi
        if ((ai > bi)); then return 1; fi
    done
    return 1
}

# parse_nc_tag FILE -> echoes the x.y.z Nextcloud version from a docker-compose
# file (ignores the "-apache" image suffix). Empty if not found.
parse_nc_tag() {
    grep -Eo 'nextcloud:[0-9]+\.[0-9]+\.[0-9]+' "$1" 2>/dev/null | head -n1 | cut -d: -f2
}

# detect_downgrade <inst_channel> <inst_ref> <tgt_channel> <tgt_ref> \
#                  <inst_nc_tag> <tgt_nc_tag>
# Echoes a human-readable reason and returns 0 when moving installed->target is
# a downgrade; returns 1 (silent) otherwise. Pure function — no I/O beyond the
# reason on stdout — so it is exhaustively unit-tested.
detect_downgrade() {
    local inst_channel="$1" inst_ref="$2" tgt_channel="$3" tgt_ref="$4"
    local inst_nc="$5" tgt_nc="$6"

    # 1. Nextcloud is the unrecoverable one — check it first and report loudest.
    if [ -n "$inst_nc" ] && [ -n "$tgt_nc" ] && version_lt "$tgt_nc" "$inst_nc"; then
        echo "Nextcloud ${inst_nc} -> ${tgt_nc} (data already migrated to ${inst_nc}; the older image will refuse to start)"
        return 0
    fi

    # 2. HomeBrain release regression. update.sh treats every non-"stable"
    #    channel (beta, dev, ...) as the bleeding edge: it builds from main,
    #    which is at or ahead of every stable tag. So any non-stable -> stable
    #    move is a downgrade by definition. stable -> stable compares the tags.
    if [ -n "$inst_channel" ]; then
        if [ "$inst_channel" != "stable" ] && [ "$tgt_channel" = "stable" ]; then
            echo "${inst_channel} (tracks main) -> stable ${tgt_ref} (main is ahead of every stable release)"
            return 0
        fi
        if [ "$inst_channel" = "stable" ] && [ "$tgt_channel" = "stable" ] \
            && [ -n "$inst_ref" ] && [ -n "$tgt_ref" ] \
            && version_lt "${tgt_ref#v}" "${inst_ref#v}"; then
            echo "release ${inst_ref} -> ${tgt_ref}"
            return 0
        fi
    fi

    return 1
}

# --- Platform Detection ---
# One probe, one record. Everything that varies by hardware keys off this: which
# llama.cpp binary we install, the flag profile we run it with, how we read GPU
# telemetry, and which host hardening we apply. Previously each of those made its
# own guess from `uname -m`, and bash and app.py disagreed about what a GPU is.
#
#   HB_ARCH         x86_64 | aarch64 | ...
#   HB_GPU_DRIVER   amdgpu | nvidia | i915 | xe | none
#   HB_GPU_BACKEND  vulkan | cuda | none
#   HB_GPU_MEMORY   discrete | unified | none   (only affects how memory is reported)
#   HB_PLATFORM_TAG "${HB_ARCH}-${HB_GPU_BACKEND}", the key used in config files
#   HAS_GPU         derived: driver != none. Unchanged meaning, unchanged callers.
#
# HB_SYSFS_ROOT prefixes every sysfs read so the fixture tests can point this at
# a fake tree — the only way any of this is verifiable without owning the hardware.
detect_platform() {
  local sysfs="${HB_SYSFS_ROOT:-}"
  HB_ARCH="$(uname -m)"
  HB_GPU_DRIVER="none"

  # Driver identity comes from the render node's bound driver, not from the
  # architecture. Display-only engines (RPi VideoCore vc4/v3d) expose a render
  # node too, so only compute-capable drivers count.
  local link drv found=""
  for link in "${sysfs}"/sys/class/drm/renderD*/device/driver; do
    [[ -L "$link" ]] || continue
    # sysfs always makes this a symlink into .../bus/pci/drivers/<name>; plain
    # readlink (no -f) keeps the fixture tests portable to a macOS dev box.
    drv="$(basename "$(readlink "$link")")"
    case "$drv" in
      amdgpu|nvidia|i915|xe) found+=" ${drv} " ;;
    esac
  done

  # Prefer a discrete card over an integrated one. On a hybrid box the iGPU
  # usually takes renderD128 and would win a first-match scan, but it is not the
  # card we want to size the model for or install a backend against.
  for drv in nvidia amdgpu xe i915; do
    if [[ "$found" == *" ${drv} "* ]]; then HB_GPU_DRIVER="$drv"; break; fi
  done

  # Fallback: a GPU that is present on the bus but whose driver failed to probe
  # has no render node. This is not hypothetical — the Navi 44 VCN ring-test bug
  # takes amdgpu down exactly that way, and the modprobe workaround that fixes it
  # is applied under HAS_GPU. Matching on vendor keeps that recovery path alive.
  # Deliberately narrower than a bare VGA-class match: a display chip from any
  # other vendor must not flip an RPi-class board into the AI stack.
  if [[ "$HB_GPU_DRIVER" == "none" ]] && command -v lspci &>/dev/null; then
    local pci
    pci="$(lspci 2>/dev/null | grep -iE "VGA|3D controller|Display controller" || true)"
    case "$pci" in
      *NVIDIA*)           HB_GPU_DRIVER="nvidia" ;;
      *AMD*|*ATI*|*Radeon*) HB_GPU_DRIVER="amdgpu" ;;
      *Intel*)            HB_GPU_DRIVER="i915" ;;
    esac
  fi

  case "$HB_GPU_DRIVER" in
    amdgpu)   HB_GPU_BACKEND="vulkan"; HB_GPU_MEMORY="discrete" ;;
    # Grace-class parts (DGX Spark, Jetson) share one LPDDR pool with the CPU;
    # aarch64 is the practical proxy for that. x86 NVIDIA is discrete VRAM.
    # This only decides how memory is *labelled* — nothing else depends on it.
    nvidia)   HB_GPU_BACKEND="cuda"
              if [[ "$HB_ARCH" == "aarch64" ]]; then
                  HB_GPU_MEMORY="unified"
              else
                  HB_GPU_MEMORY="discrete"
              fi ;;
    i915|xe)  HB_GPU_BACKEND="vulkan"; HB_GPU_MEMORY="unified" ;;
    *)        HB_GPU_BACKEND="none";   HB_GPU_MEMORY="none" ;;
  esac

  HB_PLATFORM_TAG="${HB_ARCH}-${HB_GPU_BACKEND}"
  if [[ "$HB_GPU_DRIVER" == "none" ]]; then HAS_GPU=false; else HAS_GPU=true; fi
  export HB_ARCH HB_GPU_DRIVER HB_GPU_BACKEND HB_GPU_MEMORY HB_PLATFORM_TAG HAS_GPU
}
detect_platform

# Back-compat alias. Callers that only want the boolean keep working.
detect_gpu() { detect_platform; }

# Write the record where app.py can read it without re-implementing the probe.
# Called from provision.sh and on manager start; regenerating at boot is enough,
# since the only thing that changes this is a driver that stopped loading.
emit_platform_json() {
    detect_platform
    local dest="${1:-${INSTALL_DIR}/.platform.json}"
    local json
    printf -v json '{"arch":"%s","gpu_driver":"%s","gpu_backend":"%s","gpu_memory":"%s","platform_tag":"%s","has_gpu":%s}\n' \
        "$HB_ARCH" "$HB_GPU_DRIVER" "$HB_GPU_BACKEND" "$HB_GPU_MEMORY" "$HB_PLATFORM_TAG" "$HAS_GPU"
    if [[ "$dest" == "-" ]]; then
        printf '%s' "$json"
        return 0
    fi
    printf '%s' "$json" > "$dest" 2>/dev/null || { log_warn "Could not write platform record to $dest"; return 1; }
    chmod 644 "$dest" 2>/dev/null || true
}

# --- GPU host hardening ---
# Dispatches on the detected driver. Everything in the amdgpu arm below used to
# run on any box that had a GPU at all, so an NVIDIA target would have had
# amdgpu kernel params written into its GRUB config and its initramfs rebuilt.
# The Vulkan runtime is a property of the backend, the power-management tweaks
# are a property of the driver; they are separated here for that reason.
harden_gpu() {
    # Resolve config/ relative to this file, not to the caller's SCRIPT_DIR —
    # provision.sh re-execs itself and SCRIPT_DIR has bitten us there before.
    local config_dir
    config_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../config" 2>/dev/null && pwd)" \
        || config_dir="${INSTALL_DIR}/config"

    if [[ "$HB_GPU_BACKEND" == "vulkan" ]]; then
        log_info "Installing Vulkan runtime (backend: vulkan, driver: ${HB_GPU_DRIVER})..."
        apt-get install -y -qq mesa-vulkan-drivers libvulkan1 vulkan-tools 2>/dev/null \
            || log_warn "Vulkan driver install failed. GPU inference may not work."
    fi

    case "$HB_GPU_DRIVER" in
        amdgpu) _harden_gpu_amdgpu "$config_dir" ;;
        nvidia) _harden_gpu_nvidia ;;
        none)   log_info "No compute GPU detected — skipping GPU hardening." ;;
        *)      log_info "GPU driver '${HB_GPU_DRIVER}' needs no host hardening." ;;
    esac
}

_harden_gpu_amdgpu() {
    local config_dir="$1"

    # Prevent AMD GPU runtime power management (keeps model in VRAM while idle)
    # Add amdgpu.runpm=0 and amdgpu.pg_mask=0 to GRUB if not already present
    if ! grep -q "amdgpu.runpm=0" /etc/default/grub 2>/dev/null; then
        sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 amdgpu.runpm=0 amdgpu.pg_mask=0"/' /etc/default/grub
        update-grub 2>/dev/null || true
        log_info "Disabled AMD GPU runtime PM via kernel params (requires reboot to take effect)."
    elif ! grep -q "amdgpu.pg_mask=0" /etc/default/grub 2>/dev/null; then
        sed -i 's/amdgpu.runpm=0/amdgpu.runpm=0 amdgpu.pg_mask=0/' /etc/default/grub
        update-grub 2>/dev/null || true
        log_info "Added amdgpu.pg_mask=0 to kernel params."
    fi

    # Disable GPU runtime PM immediately via sysfs (takes effect now, no reboot needed)
    local ctrl gpu_pm_applied=false
    for ctrl in /sys/class/drm/card*/device/power/control; do
        if [[ -f "$ctrl" ]]; then
            echo "on" > "$ctrl" 2>/dev/null && gpu_pm_applied=true
        fi
    done
    if [[ "$gpu_pm_applied" == "true" ]]; then
        log_info "Disabled AMD GPU runtime power management (VRAM will stay loaded)."
    fi

    # Deploy udev rule for AMD GPU runtime PM (survives hotplug/driver reload)
    cp "${config_dir}/99-amdgpu-runpm.rules" /etc/udev/rules.d/
    udevadm control --reload-rules 2>/dev/null || true
    log_info "Deployed AMD GPU udev rule to /etc/udev/rules.d/"

    # Deploy modprobe config to mask VCN/JPEG IP blocks (Navi 44 init bug — see config file).
    # The driver was probing fine until linux-firmware 20250901 / kernel 6.17 exposed a
    # VCN ring-test timeout that takes the whole probe down. We don't need video decode,
    # so masking those blocks gets gfx + compute back online for llama.cpp.
    cp "${config_dir}/homebrain-amdgpu.conf" /etc/modprobe.d/
    update-initramfs -u 2>/dev/null || true
    log_info "Deployed amdgpu modprobe config (VCN/JPEG masked) to /etc/modprobe.d/"
}

# --- Headless browser ---
# Path to the Chromium-family binary OpenClaw should drive, or empty when none
# is installed. Both the installer and the openclaw.json patcher resolve through
# here so the config can never point at a binary this box does not have.
resolve_browser_path() {
    local candidate
    for candidate in google-chrome-stable chromium chromium-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# Google publishes no arm64 Linux build of Chrome at all, so on anything that is
# not amd64 we install the distro's chromium instead. Non-fatal either way: the
# browse tool degrades, the rest of the agent is unaffected.
install_headless_browser() {
    local browser
    browser=$(resolve_browser_path || true)
    if [[ -n "$browser" ]]; then
        log_info "Headless browser already present: ${browser}"
        return 0
    fi

    local deb_arch
    deb_arch=$(dpkg --print-architecture 2>/dev/null || echo "unknown")
    if [[ "$deb_arch" == "amd64" ]]; then
        # deb rather than snap: snap confinement breaks on headless servers.
        log_info "Installing Google Chrome for headless browsing..."
        wget -q -O /tmp/google-chrome.deb \
            "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
            && apt-get install -y -qq /tmp/google-chrome.deb \
            && rm -f /tmp/google-chrome.deb \
            || log_warn "Chrome install failed."
    else
        log_info "Installing Chromium for headless browsing (Google ships no Chrome build for ${deb_arch})..."
        apt-get install -y -qq chromium 2>/dev/null \
            || apt-get install -y -qq chromium-browser 2>/dev/null \
            || log_warn "Chromium install failed."
    fi

    browser=$(resolve_browser_path || true)
    if [[ -n "$browser" ]]; then
        log_info "Headless browser available: ${browser}"
    else
        log_warn "No headless browser available. The OpenClaw browser tool will not work."
    fi
}

_harden_gpu_nvidia() {
    # Nothing invasive: the proprietary driver owns power management, and CUDA
    # needs no equivalent of the runpm/VCN workarounds. Verify and report only.
    if command -v nvidia-smi >/dev/null 2>&1; then
        log_info "NVIDIA GPU: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)"
    else
        log_warn "nvidia-smi not found. Install the NVIDIA driver stack — inference and GPU telemetry both need it."
    fi
    log_info "No host hardening applied for NVIDIA (driver stack owns power management)."
}

# --- User Management ---
# Ensure the homebrain system user exists and is in the required groups.
# Idempotent: a properly-built OS image already has this user, in which case
# this is a no-op. Generic Debian/Ubuntu installs (e.g. plain Raspberry Pi OS)
# do not, so create it here with sudo + a locked password (key-based login only).
ensure_homebrain_user() {
    if ! id -u "${HOMEBRAIN_USER}" >/dev/null 2>&1; then
        log_info "Creating system user '${HOMEBRAIN_USER}'..."
        useradd -m -s /bin/bash "${HOMEBRAIN_USER}"
        # Locked password: SSH key-based access only, but sudo via NOPASSWD is
        # NOT granted here — admin still drives privileged ops.
        passwd -l "${HOMEBRAIN_USER}" >/dev/null 2>&1 || true
        if getent group sudo >/dev/null 2>&1; then
            usermod -aG sudo "${HOMEBRAIN_USER}" 2>/dev/null || true
        fi
        mkdir -p "${HOMEBRAIN_HOME}/.ssh"
        chmod 700 "${HOMEBRAIN_HOME}/.ssh"
        chown -R "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "${HOMEBRAIN_HOME}/.ssh"
    fi
    # On modern Ubuntu, useradd -m creates $HOME with mode 0700, which blocks
    # other UIDs (notably www-data UID 33 inside the Nextcloud container) from
    # traversing into bind-mounted subdirs like ${HOME}/nextcloud-data. 0755
    # exposes only directory traversal; .ssh stays 0700 by its own perms.
    chmod 0755 "${HOMEBRAIN_HOME}" 2>/dev/null || true
    for grp in docker render video; do
        if getent group "$grp" >/dev/null 2>&1; then
            usermod -aG "$grp" "${HOMEBRAIN_USER}" 2>/dev/null || true
        fi
    done
    ensure_homebrain_sudo
    ensure_mcp_audit_logs
}

# Passwordless sudo for the homebrain user.
#
# This looks like a widening and is not one. homebrain is in the `docker` group
# (above), and any docker group member can `docker run -v /:/host` — root, by a
# longer route. The account had a locked password and no sudoers rule, so the
# agent that HomeBrain's "you never need a shell" promise depends on could not
# run a privileged command at all, while still being one container away from
# root. That gap bought no safety and pushed the agent toward the escape hatch.
#
# Make it explicit instead. The real boundary is the Telegram allowFrom pairing
# and the loopback-bound gateway — see the trust-boundary note in AGENTS.md.
ensure_homebrain_sudo() {
    local dest="/etc/sudoers.d/homebrain"
    local tmp
    tmp="$(mktemp)" || return 0
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${HOMEBRAIN_USER}" > "$tmp"
    # Never install an unvalidated sudoers fragment: a malformed file in
    # /etc/sudoers.d can lock every user out of sudo on the whole box.
    if visudo -c -f "$tmp" >/dev/null 2>&1; then
        install -m 0440 -o root -g root "$tmp" "$dest"
        log_info "Passwordless sudo enabled for ${HOMEBRAIN_USER}."
    else
        log_warn "Generated sudoers fragment failed validation — not installed."
    fi
    rm -f "$tmp"
}

# .env holds MASTER_PASSWORD, every service credential, the vault admin token
# and the off-site password. It must be root-only.
#
# On .58 it was found homebrain:homebrain 0600 — owned by the very account the
# agent runs as, so reading the master password needed nothing more than `cat`.
# No code path in the tree explains that, so assert the state rather than hunt
# the cause. This does not contain a root-equivalent agent; it restores the
# intended asymmetry and still stops a container escape landing as www-data.
harden_env_file() {
    [[ -f "$ENV_FILE" ]] || return 0
    chown root:root "$ENV_FILE" 2>/dev/null || true
    chmod 600 "$ENV_FILE" 2>/dev/null || true
}

# The MCP servers run as the agent user and append their audit trail here.
# /var/log/homebrain is root:root 0755, so without this every audit write fails
# — silently, because mcp_common.audit() swallows the error after also writing
# to stderr. The effect was that `vault.reveal`, the most sensitive call in the
# system and one INTEGRATIONS_PLAN describes as "every call audited", left
# nothing on disk.
#
# Pre-create the files owned by the agent rather than opening up the directory:
# appending needs write on the *file*, while unlinking or replacing it needs
# write on the *directory*. Keeping the directory root-owned means the agent can
# add to its own audit trail but cannot remove it.
ensure_mcp_audit_logs() {
    local server f
    mkdir -p "$LOG_DIR" 2>/dev/null || return 0
    for server in vault nextcloud homeassistant homebrain email; do
        f="${LOG_DIR}/mcp-${server}-audit.log"
        [[ -e "$f" ]] || : > "$f" 2>/dev/null || continue
        chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "$f" 2>/dev/null || true
        chmod 640 "$f" 2>/dev/null || true
    done
}

# --- Admin user creation (Ubuntu x86 doesn't ship with a default user) ---
ensure_admin_user() {
    if id -u admin >/dev/null 2>&1; then
        log_info "admin user already exists."
        return 0
    fi
    log_info "Creating admin user for Ubuntu x86..."
    useradd -m -s /bin/bash admin
    mkdir -p /home/admin/.ssh
    chmod 700 /home/admin/.ssh
    # Add to render/video for GPU access, docker for container management
    for grp in render video docker; do
        if getent group "$grp" >/dev/null 2>&1; then
            usermod -aG "$grp" admin 2>/dev/null || true
        fi
    done
}

# --- Environment Loading ---
load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        source "$ENV_FILE"
        set +a
    else
        die "Environment file ($ENV_FILE) not found."
    fi
    # HAS_GPU is intentionally not persisted to .env (provision sets it in the
    # running shell only). The .env.template ships an empty HAS_GPU= line, which
    # the source above would happily clobber any detected value with. Re-detect
    # whenever it comes back empty so downstream gates (auto AI setup, backup
    # AI snapshots, llama updates) see the correct value.
    if [[ -z "${HAS_GPU:-}" ]]; then
        detect_gpu
    fi
}

# --- Resilience Helpers ---
check_internet() {
    # Some networks rate-limit ICMP to specific hosts (we've seen 8.8.8.8 silently
    # dropped while 1.1.1.1 succeeds), and root vs unprivileged ping take different
    # socket paths. Try a couple of hosts, then fall back to TCP/HTTPS to GitHub —
    # which is what we'll actually need for downloads anyway.
    ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 \
        || ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 \
        || curl -sf --max-time 5 -o /dev/null https://github.com
}

wait_for_time_sync() {
    # Robustness: Block setup until system time is valid (Year >= 2023)
    # Critical for SSL certificates, Oauth, and TOTP.
    if [[ $(date +%Y) -lt 2026 ]]; then
        log_info "System time invalid ($(date)). Waiting for NTP sync..."
        # Attempt to force systemd-timesyncd
        systemctl restart systemd-timesyncd || true
        
        local retries=0
        while [[ $(date +%Y) -lt 2026 ]]; do
            sleep 5
            ((retries++))
            if [[ $retries -gt 24 ]]; then log_warn "Time sync timed out. SSL/OAuth may fail."; break; fi
        done
        log_info "Time synchronized: $(date)"
    fi
}


# --- Configuration Helpers ---
update_env_var() {
    local key="$1"
    local value="$2"
    
    if [[ -f "$ENV_FILE" ]]; then
        # If key exists, replace it
        if grep -q "^${key}=" "$ENV_FILE"; then
            # Escape value for sed (basic safety for URLs/domains)
            local safe_val
            safe_val=$(printf '%s\n' "$value" | sed -e 's/[\/&]/\\&/g')
            sed -i "s|^${key}=.*|${key}='${safe_val}'|" "$ENV_FILE"
        else
            # If key missing, append it. Ensure the file ends with a newline
            # first — .env.template ships without a trailing \n on the last
            # entry, and `>>` does not prepend one, which merged consecutive
            # appends onto the previous line (e.g. HA_BASE_URL=VAULT_LAN_IP=…).
            [[ -s "$ENV_FILE" && "$(tail -c1 "$ENV_FILE")" != $'\n' ]] && echo "" >> "$ENV_FILE"
            echo "${key}='${value}'" >> "$ENV_FILE"
        fi
    else
        log_warn ".env file not found, creating new one."
        echo "${key}='${value}'" > "$ENV_FILE"
    fi
    # Every bash write path funnels through here, and the shell ones create the
    # file under the default umask (0644). Re-assert after each write.
    harden_env_file
}

# Read a single value out of .env. Deliberately takes the LAST match: the
# .env.template ships empty placeholders (HOMEBRAIN_SELF_NONCE=, etc.) that the
# dashboard later appends real values for, so a naive first-match grep returns
# the empty one. Surrounding quotes (update_env_var writes them) are stripped.
env_value() {
    local key="$1" v
    [[ -f "$ENV_FILE" ]] || return 0
    v="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1)"
    v="${v#*=}"
    v="${v%\"}"; v="${v#\"}"
    v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

# Merge an archive's portable instance secrets (instance_secrets.env, written
# by backup.sh) into .env. Used by restore.sh before any container starts.
#
# Splits each line on the FIRST '=' only. `IFS='=' read -r key value` looks
# equivalent, but it consumes a *trailing* '=' as a field delimiter — and a
# Fernet key is base64 of 32 bytes, so it always ends in exactly one. That cut
# HOMEBRAIN_EMAIL_KEY from 44 characters to 43 on every single restore. Fernet
# then rejects the key outright, which left every stored account token
# undecryptable: the block that exists to carry the key across a restore was
# destroying it instead. See scripts/tests/test_instance_secrets.sh.
merge_instance_secrets() {
    local src="$1" line key value
    [[ -f "$src" ]] || return 0
    while read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        [[ -n "$key" ]] || continue
        # Strip any surrounding quotes the value picked up on the way out.
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        update_env_var "$key" "$value"
        log_info "  imported ${key}"
    done < "$src"
}

# --- Self-MCP bearer token -------------------------------------------------
# The homebrain-self MCP server authenticates to the dashboard with a bearer
# token derived from the master password:
#
#     token = HMAC-SHA256(key=HOMEBRAIN_SELF_NONCE, msg=MASTER_PASSWORD)
#
# The dashboard recomputes it from .env on every request (src/integrations.py:
# _self_token) while the MCP server reads a cached copy from
# ~/.openclaw/homebrain.token. Any path that changes MASTER_PASSWORD or
# HOMEBRAIN_SELF_NONCE must refresh that file or every homebrain-self__* tool
# call starts 401ing — silently, because nothing else reads it.

# Pure derivation, kept separate so the unit test can pin it against the
# Python implementation without touching the filesystem.
derive_self_token() {
    local nonce="$1" password="$2"
    [[ -n "$nonce" && -n "$password" ]] || return 1
    printf '%s' "$password" \
        | openssl dgst -sha256 -hmac "$nonce" -r 2>/dev/null \
        | cut -d' ' -f1
}

# Re-derive ~/.openclaw/homebrain.token in place. Optional $1 overrides the
# password to derive from — the rotation script needs this because it calls us
# with the NEW password, which .env may not carry yet. Best-effort by design:
# a box with no OpenClaw has no self-MCP, and a failure here must never strand
# a rotation or a restore.
refresh_self_token() {
    local new_pass="${1:-}"
    local tok_dir="${HOMEBRAIN_HOME}/.openclaw"
    local tok_file="${SELF_TOKEN_FILE:-${tok_dir}/homebrain.token}"
    [[ -d "$tok_dir" ]] || return 0   # no OpenClaw on this box — nothing to do

    local mp nonce tok
    mp="${new_pass:-$(env_value MASTER_PASSWORD)}"
    nonce="$(env_value HOMEBRAIN_SELF_NONCE)"
    if [[ -z "$mp" || -z "$nonce" ]]; then
        log_warn "Self-MCP token not re-derived: MASTER_PASSWORD or HOMEBRAIN_SELF_NONCE missing."
        return 1
    fi
    if ! tok="$(derive_self_token "$nonce" "$mp")" || [[ -z "$tok" ]]; then
        log_warn "Self-MCP token derivation failed (openssl missing?) — agent self-tools may 401."
        return 1
    fi

    # Subshell so the restrictive umask cannot leak into the caller's shell.
    if ! ( umask 077; printf '%s\n' "$tok" > "$tok_file" ); then
        log_warn "Could not write ${tok_file} — agent self-tools may 401."
        return 1
    fi
    chmod 600 "$tok_file" 2>/dev/null || true
    chown "${HOMEBRAIN_USER}:${HOMEBRAIN_USER}" "$tok_file" 2>/dev/null || true
    log_info "Self-MCP bearer token re-derived."
}

# --- Docker Helpers ---
# Helper to get all active compose files
get_compose_args() {
    local args="-f $COMPOSE_FILE"
    if [[ -f "$OVERRIDE_FILE" ]]; then
        args="$args -f $OVERRIDE_FILE"
    fi
    echo "$args"
}

get_nc_cid() {
    docker compose $(get_compose_args) ps -a -q nextcloud 2>/dev/null || true
}

get_ha_cid() {
    docker compose $(get_compose_args) ps -a -q homeassistant 2>/dev/null || true
}

get_nc_db_cid() {
    docker compose $(get_compose_args) ps -a -q db 2>/dev/null || true
}

get_vault_cid() {
    docker compose $(get_compose_args) ps -a -q vaultwarden 2>/dev/null || true
}

is_stack_running() {
    local nc_cid=$(get_nc_cid)
    local ha_cid=$(get_ha_cid)
    # Returns true only if both Nextcloud and Home Assistant container IDs are found and are running
    [[ -n "$nc_cid" ]] && [[ $(docker inspect -f '{{.State.Running}}' "$nc_cid" 2>/dev/null) == "true" ]] && \
    [[ -n "$ha_cid" ]] && [[ $(docker inspect -f '{{.State.Running}}' "$ha_cid" 2>/dev/null) == "true" ]]
}

# --- Tunnel Profiles Helper ---
get_tunnel_profiles() {
    local profiles=""
    # 1. Sanitize Inputs (Trim Whitespace) to prevent false positives
    local p_endpoint="${PANGOLIN_ENDPOINT:-}"; p_endpoint="${p_endpoint//[[:space:]]/}"
    local p_id="${NEWT_ID:-}"; p_id="${p_id//[[:space:]]/}"
    local p_secret="${NEWT_SECRET:-}"; p_secret="${p_secret//[[:space:]]/}"
    local cf_nc_token="${CF_TOKEN_NC:-}"; cf_nc_token="${cf_nc_token//[[:space:]]/}"
    local cf_ha_token="${CF_TOKEN_HA:-}"; cf_ha_token="${cf_ha_token//[[:space:]]/}"

    # 2. Determine Mode (custom Cloudflare prioritized over Pangolin)
    # We enforce mutual exclusivity: If Cloudflare tokens are provided, we ignore Pangolin tokens.

    if [[ -n "$cf_nc_token" ]] || [[ -n "$cf_ha_token" ]]; then
        # --- Cloudflare Mode ---
        if [[ -n "$cf_nc_token" ]]; then
            profiles="${profiles} --profile cloudflare-nc"
        fi
        if [[ -n "$cf_ha_token" ]]; then
            profiles="${profiles} --profile cloudflare-ha"
        fi
    elif [[ -n "$p_endpoint" ]] && [[ -n "$p_id" ]] && [[ -n "$p_secret" ]]; then
        # --- Pangolin Mode ---
        profiles="--profile pangolin"
    else
        log_info "No complete tunnel configuration found. Deploying local-only."
    fi

    # Trim leading space if any
    profiles="${profiles#" "}"

    echo "${profiles}"
}

# Caddy (LAN HTTPS edge) now always runs — no profile gate.
# Kept as a stub so deploy.sh / redeploy_tunnels.sh don't break.
get_vault_profiles() {
    echo ""
}

# Returns 0 (true) when running in local/LAN-only mode.
# Logic: local if Pangolin not provisioned (credentials absent), OR if user explicitly
# set DEPLOYMENT_MODE=local to opt out despite having credentials.
# When Pangolin IS provisioned, tunnel is on by default (DEPLOYMENT_MODE defaults to remote).
is_local_mode() {
    # No Pangolin credentials at all — always local regardless of DEPLOYMENT_MODE
    [[ -z "${NEWT_ID:-}" || -z "${NEWT_SECRET:-}" || -z "${PANGOLIN_DOMAIN:-}" ]] && return 0
    # Credentials present but user explicitly opted out
    [[ "${DEPLOYMENT_MODE:-remote}" == "local" ]] && return 0
    return 1
}

# Best-effort confirmation that newt established the Pangolin tunnel after a
# (re)deploy. A wrong NEWT_ID/SECRET/ENDPOINT leaves the box reachable on the
# LAN but silently unreachable remotely, so we surface that loudly rather than
# letting the operator discover it via a dead public URL. Returns non-zero (and
# warns) if no successful connection shows up in newt's recent logs.
verify_newt_connected() {
    local cid
    cid=$(docker compose $(get_compose_args) ps -q newt 2>/dev/null || true)
    if [[ -z "$cid" ]]; then
        log_warn "newt container not running — remote tunnel is down (LAN access unaffected)."
        return 1
    fi
    local i
    for i in 1 2 3 4 5 6; do
        if docker logs --since 5m "$cid" 2>&1 | grep -q "Tunnel connection to server established"; then
            log_info "newt tunnel connection to Pangolin confirmed."
            return 0
        fi
        sleep 5
    done
    log_warn "newt did NOT report a successful tunnel connection within ~30s."
    log_warn "Verify NEWT_ID / NEWT_SECRET / PANGOLIN_ENDPOINT — the box is still reachable on the LAN."
    log_warn "Logs: docker logs ${cid}"
    return 1
}

# Print the Pangolin org-side resources the operator must create for a tunnel
# domain. provision.sh cannot configure the Pangolin server, and the targets are
# easy to get wrong: newt runs on the homebrain_default Docker network, so it
# reaches the service containers by NAME on their INTERNAL ports — NOT the host-
# published ports (nc's host 8080 maps to container :80; vault's 8082 is even
# loopback-only). The manager is a host process, reached via the bridge gateway.
print_pangolin_resource_guide() {
    local dom="$1"
    local gw
    gw=$(docker network inspect homebrain_default \
            --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
    gw="${gw:-172.18.0.1}"
    log_warn "Pangolin resources to configure for https://${dom} (targets are HTTP; TLS ends at the edge):"
    log_warn "  ${dom} (root, manager) -> ${gw}:80        (host process — use the bridge gateway, not a name)"
    log_warn "  nc.${dom}              -> nextcloud:80     (container internal port — NOT host 8080)"
    log_warn "  ha.${dom}              -> homeassistant:8123"
    log_warn "  vault.${dom}           -> vaultwarden:80   (host 8082 is loopback-only — use the name)"
}

wait_for_healthy() {
    local service_name="$1"
    local timeout_seconds="$2"
    local container_id

    log_info "Waiting for $service_name to become healthy..."
    
    # Retry finding container ID
    local retries=10
    while [[ $retries -gt 0 ]]; do
        container_id=$(docker compose $(get_compose_args) ps -q "$service_name" 2>/dev/null)
        if [[ -n "$container_id" ]]; then break; fi
        sleep 2
        ((retries--))
    done

    [[ -z "$container_id" ]] && return 1

    local end_time=$((SECONDS + timeout_seconds))
    while [ $SECONDS -lt $end_time ]; do
        local status
        status=$(docker inspect --format="{{if .State.Health}}{{.State.Health.Status}}{{end}}" "$container_id" 2>/dev/null || echo "unknown")
        if [ "$status" == "healthy" ]; then
            log_info "✅ $service_name is healthy."
            return 0
        fi
        sleep 3
    done
    log_error "❌ $service_name failed health check."
    return 1
}

wait_for_apt_lock() {
    local lock_files=("/var/lib/dpkg/lock" "/var/lib/dpkg/lock-frontend" "/var/lib/apt/lists/lock")
    for lock in "${lock_files[@]}"; do
        while fuser "$lock" >/dev/null 2>&1; do
            log_info "Waiting for apt lock ($lock)..."
            sleep 3
        done
    done
}

install_deps_enable_docker() {
    # Offline Fallback: If offline but docker exists, skip apt to prevent crash
    if ! check_internet; then
        if command -v docker >/dev/null; then
            log_warn "Offline mode detected. Skipping apt updates (Docker already installed)."
            return
        else
            log_warn "No internet and Docker not found. Proceeding with apt (may fail)..."
        fi
    fi

    # --- 0. Install Dependencies ---
    log_info "Installing dependencies"
    wait_for_apt_lock
    # Refresh the package lists BEFORE installing. A factory image that has sat
    # on a shelf carries apt lists old enough that Debian/Ubuntu have pruned the
    # exact .deb versions they name, and the install then dies on 404s — fatal
    # under `set -euo pipefail`, so provisioning never gets past this line.
    # Observed on the RPi4 test box (lists from March, glib2.0 404s, rc=100).
    apt-get update -qq
    local common_pkgs="ca-certificates gnupg lsb-release cron gpg rsync python3-flask python3-dotenv python3-requests python3-pip python3-venv jq moreutils pwgen git parted argon2 smartmontools unattended-upgrades"
    apt-get install -y -qq $common_pkgs

    # Headless browser for the OpenClaw browser tool (non-fatal)
    if [[ "$HAS_GPU" == "true" ]]; then
        install_headless_browser
    fi

    # Docker setup
    if ! [ -f /etc/apt/keyrings/docker.gpg ]; then
        mkdir -p /etc/apt/keyrings
        local os_id
        os_id=$(. /etc/os-release && echo "$ID")  # "debian" or "ubuntu"
        curl -fsSL "https://download.docker.com/linux/${os_id}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${os_id} $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
        apt-get update -y -qq
    fi
    
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    log_info "Starting docker service"
    systemctl enable --now docker
}

install_python_venv_deps(){
    # Install Python dependencies in venv
    VENV_DIR="$INSTALL_DIR/venv"
    
    # Optimization: If venv exists and seems valid, skip pip (Offline Safe)
    if [ -f "$VENV_DIR/bin/activate" ] && [ -d "$VENV_DIR/lib" ]; then
        if ! check_internet; then
            log_info "Offline: Using existing venv."
            return
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtualenv..."
        python3 -m venv "$VENV_DIR"
    fi
    
    # Use direct path to pip to avoid 'source' issues in strict mode (set -u)
    local venv_pip="$VENV_DIR/bin/pip"
    # Upgrade pip in venv
    "$venv_pip" install --upgrade pip
    
    # Install requirements (no conflicts)
    if [ -f "$INSTALL_DIR/requirements.txt" ]; then
        echo "Installing Python dependencies..."
        "$venv_pip" install -r "$INSTALL_DIR/requirements.txt"
    fi
}

# --- Maintenance Mode ---
set_maintenance_mode() {
    local mode="$1" # --on or --off
    local nc_cid
    nc_cid=$(get_nc_cid)
    
    if [[ -z "$nc_cid" ]]; then return 1; fi
    
    log_info "Setting Nextcloud maintenance mode: $mode"
    docker exec -u www-data "$nc_cid" php occ maintenance:mode "$mode" || true
}

# Configures Trusted Proxies in Home Assistant configuration.yaml
configure_ha_proxy_settings() {
    local subnet="$1"
    local cid="$2"

    log_info "Configuring Home Assistant trusted proxies for subnet: $subnet"

    docker exec "$cid" sh -c "
        CONF='/config/configuration.yaml'
        # 1. Check if the subnet is already trusted
        if grep -Fq '$subnet' \"\$CONF\"; then
            echo 'Subnet already trusted.'
        else
            # 2. Check if trusted_proxies block exists
            if grep -q 'trusted_proxies:' \"\$CONF\"; then
                # Append to existing list
                sed -i '/trusted_proxies:/a \    - $subnet' \"\$CONF\"
            # 3. Check if http block exists but no proxies
            elif grep -q '^http:' \"\$CONF\"; then
                sed -i '/^http:/a \  use_x_forwarded_for: true\n  trusted_proxies:\n    - $subnet' \"\$CONF\"
            # 4. No http block at all
            else
                echo '
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - $subnet
' >> \"\$CONF\"
            fi
        fi
    "
}

configure_nc_ha_proxy_settings() {
    log_info "Configuring trusted proxies for Docker Subnet..."
    local nc_cid=$(get_nc_cid)
    local ha_cid=$(get_ha_cid)
    
    # Get Docker Bridge Subnet
    local subnet
    # Try to find the network used by nextcloud
    if [[ -n "$nc_cid" ]]; then
        local net_name=$(docker inspect "$nc_cid" --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')
        if [[ -n "$net_name" ]]; then
            subnet=$(docker network inspect "$net_name" --format='{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
        fi
    fi
    
    # Fallback default if detection fails
    if [[ -z "$subnet" ]]; then
        subnet="172.16.0.0/12"
    fi
    log_info "Detected Docker Subnet: $subnet"

    # 1. Update Nextcloud Trusted Proxies
    if [[ -n "$nc_cid" ]]; then
        if is_local_mode; then
            # Local mode: HTTP only, trust LAN addresses, no tunnel domain required
            local lan_ip
            lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
            docker exec --user www-data "$nc_cid" php occ config:system:set overwriteprotocol --value=http || die "Failed to set overwriteprotocol."
            docker exec --user www-data "$nc_cid" php occ config:system:set overwrite.cli.url --value="http://homebrain.local:8080" || true
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 1 --value="localhost" || die "Failed to set trusted_domains localhost."
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 2 --value="homebrain.local" || die "Failed to set trusted_domains homebrain.local."
            if [[ -n "$lan_ip" ]]; then
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 3 --value="$lan_ip" || die "Failed to set trusted_domains LAN IP."
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 4 --value="${lan_ip}:8080" || true
            fi
            # If an explicit trusted domain was set anyway, honour it at slot 5
            if [[ -n "${NEXTCLOUD_TRUSTED_DOMAINS:-}" ]]; then
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 5 --value="$NEXTCLOUD_TRUSTED_DOMAINS" || true
            fi
        else
            # Remote mode: HTTPS via tunnel + LAN access via homebrain.local / LAN IP
            local lan_ip
            lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
            docker exec --user www-data "$nc_cid" php occ config:system:set overwriteprotocol --value=https || die "Failed to set overwriteprotocol."
            docker exec --user www-data "$nc_cid" php occ config:system:set overwrite.cli.url --value="https://${NEXTCLOUD_TRUSTED_DOMAINS}" || true
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 1 --value="$NEXTCLOUD_TRUSTED_DOMAINS" || die "Failed to set trusted_domains 1."
            docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 2 --value="homebrain.local" || true
            if [[ -n "$lan_ip" ]]; then
                docker exec --user www-data "$nc_cid" php occ config:system:set trusted_domains 3 --value="$lan_ip" || true
            fi
        fi
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 0 --value="$TRUSTED_PROXIES_0" || die "Failed to set trusted_proxies 0."
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 1 --value="$TRUSTED_PROXIES_1" || die "Failed to set trusted_proxies 1."
        # Use index 10 to avoid conflict with existing static ones
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 10 --value="$subnet" || die "Failed to set trusted_proxies 10."
        # Also ensure localhost is trusted
        docker exec --user www-data "$nc_cid" php occ config:system:set trusted_proxies 11 --value="127.0.0.1" || die "Failed to set trusted_proxies 11."
    fi

    # 2. Update Home Assistant Trusted Proxies
    if [[ -n "$ha_cid" ]]; then
        configure_ha_proxy_settings "$subnet" "$ha_cid"
    fi
}

# nc_status_needs_upgrade — reads `occ status` output on stdin and returns 0
# (true) when a Nextcloud DB schema upgrade is pending. Pure/testable; the
# docker plumbing lives in reconcile_nextcloud below.
nc_status_needs_upgrade() {
    grep -qiE 'needsDbUpgrade:[[:space:]]*true'
}

# reconcile_nextcloud — run Nextcloud's pending DB schema migration if needed.
#
# A docker image bump only copies the new code into the html volume; the schema
# migration still has to run. The stock image entrypoint auto-runs `occ upgrade`
# on container (re)creation, but that is skipped when the image tag is unchanged
# (compose doesn't recreate the container) and can be left incomplete after a
# downgrade-recovery roll-forward. Either way Nextcloud is stranded on its
# "Please use the command line updater because updating via browser is disabled
# in config.php" page — the docker image disables the web updater on purpose.
#
# Running it here makes the appliance self-heal without the user ever needing a
# shell. Idempotent: a no-op (just an `occ status` probe) when nothing is pending.
reconcile_nextcloud() {
    local nc_cid
    nc_cid=$(get_nc_cid)
    if [[ -z "$nc_cid" ]]; then
        log_warn "Nextcloud container not found; skipping schema reconcile."
        return 0
    fi

    # occ is unavailable until the entrypoint finishes copying code on a fresh
    # image, so poll briefly for a usable status before deciding.
    local status="" tries=0
    while ((tries < 30)); do
        if status=$(docker exec -u www-data "$nc_cid" php occ status 2>/dev/null) && [[ -n "$status" ]]; then
            break
        fi
        status=""
        tries=$((tries + 1))
        sleep 2
    done

    if [[ -z "$status" ]]; then
        log_warn "Nextcloud occ not responsive; skipping schema reconcile."
        return 0
    fi

    # Run `occ upgrade` unconditionally (matching restore.sh): it is a fast no-op
    # when nothing is pending ("Nextcloud is already latest version"), so the
    # repair never depends on parsing the exact status field — whatever left the
    # instance needing a migration, this clears it. nc_status_needs_upgrade is
    # used only to phrase the log.
    if printf '%s' "$status" | nc_status_needs_upgrade; then
        log_info "Nextcloud reports a pending DB schema upgrade — running occ upgrade..."
    else
        log_info "Reconciling Nextcloud schema (occ upgrade; no-op if already current)..."
    fi
    docker exec -u www-data "$nc_cid" php occ upgrade \
        || log_warn "occ upgrade returned non-zero (often 'no upgrade required') — check Nextcloud logs."
    docker exec -u www-data "$nc_cid" php occ maintenance:mode --off >/dev/null 2>&1 || true
    log_info "Nextcloud schema reconcile complete."
}

configure_nextcloud_redis() {
    local nc_cid=$(get_nc_cid)
    
    if [[ -z "$nc_cid" ]]; then 
        log_warn "Nextcloud container not found. Skipping Redis config."
        return 1
    fi

    log_info "Configuring Redis for Nextcloud..."
    
    # We use '|| true' on some commands to prevent a hard failure if the config is already set,
    # though 'occ config:system:set' is generally idempotent.
    
    # 1. Configure Connection Details
    docker exec --user www-data "$nc_cid" php occ config:system:set redis host --value="redis" || return 1
    docker exec --user www-data "$nc_cid" php occ config:system:set redis port --value=6379 --type=integer
    
    # 2. Configure Caching Backends
    # Distributed cache (Redis)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.distributed --value="\OC\Memcache\Redis"
    # Locking (Redis is much faster than DB locking)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.locking --value="\OC\Memcache\Redis"
    # Local Cache (APCu is faster for local, but Redis is acceptable if APCu is missing. We prefer APCu)
    docker exec --user www-data "$nc_cid" php occ config:system:set memcache.local --value="\OC\Memcache\APCu"

    log_info "Redis configuration applied successfully."
}

# --- Backup storage ---------------------------------------------------------
# Make $BACKUP_MOUNTDIR usable, honouring the no-drive mode.
#
# BACKUP_INTERNAL=true means BACKUP_MOUNTDIR is a path on the root disk (the
# archives are the staging set for the off-site mirror, which is the actual
# protection there), so "not a mountpoint" is the normal state. Otherwise the
# mount check is mandatory: a USB drive that fell off must never silently
# degrade into filling the root disk.
#
# backup.sh and restore.sh both need this and used to carry separate copies.
# They drifted — restore.sh never learned about BACKUP_INTERNAL (added in
# #123), so no-drive boxes could back up and could never restore. One
# definition, two callers.
# Archives in $1 that the emergency prune may delete, oldest first.
#
# Deliberately excludes the newest archive, always. The prune runs *before*
# this backup exists, and this backup can still fail — a bad dump, or a failed
# verify, which deletes the new archive. Freeing space by deleting the only
# known-good copy leaves the user with less protection than they started with,
# so the last one is never a candidate. Empty output means "nothing may be
# pruned", which the caller must treat as a hard stop, not as "nothing found".
prunable_archives() {
    local dir="$1"
    find "$dir" -maxdepth 1 -type f \
        \( -name "homebrain_backup*.tar.gz*" -o -name "nextcloud_backup*.tar.gz*" \) \
        -printf "%T@ %p\n" 2>/dev/null \
        | sort -n | awk '{print $2}' | sed '$d'
}

ensure_backup_dir() {
    if [[ "${BACKUP_INTERNAL:-false}" == "true" ]]; then
        mkdir -p "$BACKUP_MOUNTDIR" \
            || die "Cannot create backup directory $BACKUP_MOUNTDIR."
    elif ! mountpoint -q "$BACKUP_MOUNTDIR"; then
        log_info "Attempting to mount $BACKUP_MOUNTDIR..."
        mount "$BACKUP_MOUNTDIR" || die "Failed to mount backup drive."
    fi
}

# --- Off-site backup copy ---------------------------------------------------
# One rclone remote named "offsite", defined entirely by the OFFSITE_* vars
# from .env — no rclone.conf to manage. Credentials travel via rclone's
# RCLONE_CONFIG_* environment variables (and stdin for `obscure`), never argv.

offsite_env() {
    case "${OFFSITE_TYPE:-}" in
        sftp)
            local host="${OFFSITE_HOST}" port=""
            if [[ "$host" == *:* ]]; then port="${host##*:}"; host="${host%%:*}"; fi
            export RCLONE_CONFIG_OFFSITE_TYPE=sftp
            export RCLONE_CONFIG_OFFSITE_HOST="$host"
            [[ -n "$port" ]] && export RCLONE_CONFIG_OFFSITE_PORT="$port"
            export RCLONE_CONFIG_OFFSITE_USER="${OFFSITE_USER}"
            RCLONE_CONFIG_OFFSITE_PASS=$(printf '%s' "${OFFSITE_PASS}" | rclone obscure -) || return 1
            export RCLONE_CONFIG_OFFSITE_PASS
            ;;
        webdav)
            export RCLONE_CONFIG_OFFSITE_TYPE=webdav
            export RCLONE_CONFIG_OFFSITE_URL="${OFFSITE_HOST}"
            # Nextcloud speaks chunked upload — required for multi-GB archives.
            if [[ "${OFFSITE_HOST}" == *remote.php* ]]; then
                export RCLONE_CONFIG_OFFSITE_VENDOR=nextcloud
            else
                export RCLONE_CONFIG_OFFSITE_VENDOR=other
            fi
            export RCLONE_CONFIG_OFFSITE_USER="${OFFSITE_USER}"
            RCLONE_CONFIG_OFFSITE_PASS=$(printf '%s' "${OFFSITE_PASS}" | rclone obscure -) || return 1
            export RCLONE_CONFIG_OFFSITE_PASS
            ;;
        s3)
            export RCLONE_CONFIG_OFFSITE_TYPE=s3
            export RCLONE_CONFIG_OFFSITE_PROVIDER=Other
            export RCLONE_CONFIG_OFFSITE_ENDPOINT="${OFFSITE_HOST}"
            export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="${OFFSITE_USER}"
            export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="${OFFSITE_PASS}"
            ;;
        *)
            log_warn "Unknown off-site type: '${OFFSITE_TYPE:-}'"
            return 1
            ;;
    esac
}

# Mirror the local archive set to the remote.
#
# Full/data-only archives (multi-GB) are scoped to the single newest one
# before every copy. Local retention keeps the last 2 around for its own
# reasons (backup.sh: "Keep: 2") — without this scoping, the second-newest
# would get re-uploaded and immediately pruned again on every hourly resume
# tick for as long as it sits on the drive, burning a home uplink on a
# transfer that is deleted the moment it lands. System snapshots (~70MB) are
# cheap, so they keep the simpler blanket copy + age-based window.
offsite_sync() {
    command -v rclone >/dev/null || { log_warn "rclone is not installed."; return 1; }
    # At point of use, because that is the only place that reaches every box.
    # app.py calls ensure_rclone when off-site settings are SAVED, which a box
    # that already had off-site configured never does again — so the boxes most
    # in need of the chunking fix would be exactly the ones that never got it,
    # and their mirrors would go on failing with 413 forever. No-ops in
    # milliseconds once rclone is current.
    ensure_rclone "$(jq -r '.rclone.version // empty' \
        "${INSTALL_DIR}/config/versions.json" 2>/dev/null || echo "")" || true
    offsite_env || return 1
    local remote="offsite:${OFFSITE_PATH:-homebrain-backups}"

    # copy, NOT sync. sync mirrors deletions, so anything that removes a local
    # archive — a failed drive, ransomware, the emergency prune in backup.sh —
    # erased the off-site copy on the very next run. That is how a backup stops
    # being a backup: the one event the off-site copy exists for was also the
    # event that destroyed it. copy only ever adds.
    #
    # Leading / anchors the patterns to the drive's top level and --max-depth
    # stops recursion — same scope as local retention's `find -maxdepth 1`.
    # Without both, archives inside subdirectories would get mirrored too.
    # nextcloud_backup* is the legacy full-archive name, and backup.sh's local
    # retention counts it in the same keep-N pool as homebrain_backup* — so it
    # is a full backup here too. Leaving it out of this selection (and out of
    # the prune below, as an earlier revision did) copies legacy archives
    # off-site and then never prunes them: the remote grows without bound.
    local newest_full
    newest_full=$(find "$BACKUP_MOUNTDIR" -maxdepth 1 -type f \
        \( -name 'homebrain_backup*.tar.gz*' -o -name 'nextcloud_backup*.tar.gz*' \) \
        ! -name 'homebrain_backup_system_*' \
        -printf '%T@ %f\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
    # --no-update-modtime: without it, a file that is still sitting locally
    # gets its remote ModTime silently bumped to "now" on every sync even
    # though nothing was transferred (rclone's own behavior — tested live,
    # logged as "Updated modification time in destination"). Both retention
    # passes below key off ModTime, so that refresh would permanently reset
    # the age clock for as long as local retention keeps the file around.
    if [[ -n "$newest_full" ]]; then
        rclone copy "$BACKUP_MOUNTDIR" "$remote" --no-update-modtime \
            --max-depth 1 --include "/${newest_full}" || return 1
    fi
    rclone copy "$BACKUP_MOUNTDIR" "$remote" --no-update-modtime \
        --max-depth 1 \
        --include '/homebrain_backup_system_*.tar.gz*' || return 1

    # Off-site only ever needs the latest full archive — restore.sh always
    # offers the newest available — so this prunes down to one instead of
    # tracking an age window (a superseded archive could otherwise sit
    # off-site, having cost upload bandwidth, until OFFSITE_KEEP_DAYS passed).
    #
    # Gated on there BEING a local full archive. With none, the copy above was
    # a no-op and the newest remote archive is the only copy of the user's data
    # left anywhere — pruning then would delete good older archives at exactly
    # the moment the local drive is gone, i.e. a local failure propagating into
    # a remote deletion. That is the class of bug the copy-not-sync rule above
    # exists to prevent, and it must not sneak back in through retention.
    if [[ -n "$newest_full" ]]; then
        offsite_prune_full "$remote"
    fi

    # Bound system snapshots on their own schedule instead of tracking local
    # state, so a local deletion can never propagate. Never touch anything on
    # the remote that HomeBrain did not put there.
    rclone delete "$remote" \
        --min-age "${OFFSITE_KEEP_DAYS:-90}d" \
        --max-depth 1 \
        --include '/homebrain_backup_system_*.tar.gz*' 2>/dev/null \
        || log_warn "Off-site retention pass failed (copies are safe; remote may grow)."
}

# Delete every off-site full/data-only archive except the newest (by remote
# ModTime). rclone has no "keep newest N" primitive, so this lists, sorts, and
# removes the rest one file at a time. System snapshots are excluded — they
# keep the age-based window in offsite_sync above.
offsite_prune_full() {
    local remote="$1" listing
    # --filter, not --include/--exclude together: rclone's own warning calls
    # that combination "indeterminate" order, and it is not kidding — tested
    # live, it let a system snapshot's ModTime win the "newest" comparison
    # below and get the newest FULL archive deleted as superseded. --filter
    # rules apply in the given order with a first-match-wins.
    listing=$(rclone lsjson "$remote" --files-only --max-depth 1 \
        --filter '- /homebrain_backup_system_*.tar.gz*' \
        --filter '+ /homebrain_backup*.tar.gz*' \
        --filter '+ /nextcloud_backup*.tar.gz*' \
        --filter '- *' 2>/dev/null) || return 0
    jq -r 'sort_by(.ModTime) | reverse | .[1:][].Path' <<<"$listing" 2>/dev/null \
        | while IFS= read -r old; do
            [[ -n "$old" ]] || continue
            rclone deletefile "${remote}/${old}" \
                || log_warn "Could not prune superseded off-site archive: $old"
        done
}

# Install a chunk-capable rclone, replacing the distro package if needed.
#
# Ubuntu ships rclone 1.60.1 (2022), which has no Nextcloud chunked-upload
# support: it PUTs an archive as one request, and the receiving Nextcloud's
# Apache rejects anything past its body limit with "413 Request Entity Too
# Large". Small system snapshots (~68 MB) squeak through, multi-GB full
# archives never do — so the off-site copy silently ends up holding everything
# EXCEPT the user's files, which is the one thing it exists to hold.
# Nextcloud chunking landed in rclone 1.64.
#
# Installs to /usr/local/bin, which precedes /usr/bin in PATH, so the distro
# package can stay where it is.
RCLONE_MIN_MAJOR=1
RCLONE_MIN_MINOR=64
ensure_rclone() {
    local want_ver="${1:-}"
    if command -v rclone >/dev/null 2>&1; then
        local cur major minor
        cur=$(rclone version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        major="${cur%%.*}"; minor="${cur##*.}"
        if [[ -n "$cur" ]] && { [[ "$major" -gt "$RCLONE_MIN_MAJOR" ]] || \
            { [[ "$major" -eq "$RCLONE_MIN_MAJOR" ]] && [[ "$minor" -ge "$RCLONE_MIN_MINOR" ]]; }; }; then
            return 0
        fi
        log_warn "rclone ${cur:-unknown} cannot chunk uploads to Nextcloud (needs >= ${RCLONE_MIN_MAJOR}.${RCLONE_MIN_MINOR}); installing a current build."
    fi

    # Installing needs root. Bail before spending a download we cannot use —
    # this also keeps the function inert for unprivileged callers and tests.
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        log_warn "rclone needs updating but this is not running as root — skipping."
        return 1
    fi

    local arch url tmp
    case "$(uname -m)" in
        x86_64)  arch=amd64 ;;
        aarch64) arch=arm64 ;;
        *) log_warn "No rclone build for $(uname -m)."; return 1 ;;
    esac
    if [[ -n "$want_ver" ]]; then
        url="https://downloads.rclone.org/v${want_ver}/rclone-v${want_ver}-linux-${arch}.zip"
    else
        url="https://downloads.rclone.org/rclone-current-linux-${arch}.zip"
    fi

    tmp=$(mktemp -d) || return 1
    if ! curl -fsSL --retry 3 -o "$tmp/rclone.zip" "$url"; then
        rm -rf "$tmp"; log_warn "Could not download rclone from $url"; return 1
    fi
    if ! unzip -q -o "$tmp/rclone.zip" -d "$tmp"; then
        rm -rf "$tmp"; log_warn "Could not unpack the rclone download."; return 1
    fi
    local bin
    bin=$(find "$tmp" -type f -name rclone | head -1)
    if [[ -z "$bin" ]]; then
        rm -rf "$tmp"; log_warn "No rclone binary in the download."; return 1
    fi
    install -m 0755 -o root -g root "$bin" /usr/local/bin/rclone
    rm -rf "$tmp"
    hash -r 2>/dev/null || true
    log_info "Installed rclone $(/usr/local/bin/rclone version 2>/dev/null | head -1 | awk '{print $2}') to /usr/local/bin."
}

OFFSITE_STATE_FILE="/var/lib/homebrain/offsite.json"
OFFSITE_LOCK_FILE="/var/run/homebrain-offsite.lock"
# Holds the PID of the running mirror, for readers that want to report "a
# mirror is in progress" (the dashboard's Off-site status line).
#
# Deliberately NOT the lock file: probing that with a real flock means briefly
# holding it, and a mirror starting in that window sees it taken, logs "already
# running" and returns success — a silently skipped off-site copy caused by
# looking at it. This file is only ever written by the mirror and read by
# everyone else. Lives in /var/run (tmpfs), so a reboot mid-mirror clears it
# instead of leaving a permanent phantom "syncing".
OFFSITE_RUN_FILE="/var/run/homebrain-offsite.running"

# Run the mirror under the off-site lock and record the outcome.
#
# Shared by backup.sh (right after a backup) and homebrain-offsite.timer (on
# boot and hourly). Both need identical locking and identical state-writing —
# the health check reads that state file, so two copies of this logic would
# eventually disagree about whether the off-site copy is healthy.
#
# Returns non-zero only when a mirror ran and failed; a skip because another
# mirror holds the lock is success.
offsite_mirror() {
    mkdir -p /var/lib/homebrain
    exec 201>"$OFFSITE_LOCK_FILE"
    if ! flock -n 201; then
        log_info "An off-site mirror is already running — leaving it to finish."
        return 0
    fi
    log_info "Mirroring backups off-site (${OFFSITE_TYPE:-unset})..."
    printf '%d\n' "$$" > "$OFFSITE_RUN_FILE" 2>/dev/null || true
    if offsite_sync; then
        rm -f "$OFFSITE_RUN_FILE" 2>/dev/null || true
        printf '{"ts": %d, "ok": true}\n' "$(date +%s)" > "${OFFSITE_STATE_FILE}.tmp" \
            && mv "${OFFSITE_STATE_FILE}.tmp" "$OFFSITE_STATE_FILE"
        log_info "Off-site mirror complete."
        return 0
    fi
    rm -f "$OFFSITE_RUN_FILE" 2>/dev/null || true
    printf '{"ts": %d, "ok": false}\n' "$(date +%s)" > "${OFFSITE_STATE_FILE}.tmp" \
        && mv "${OFFSITE_STATE_FILE}.tmp" "$OFFSITE_STATE_FILE"
    log_warn "OFF-SITE COPY FAILED — the local backup is fine; check the off-site settings on the Backup page."
    return 1
}

# HomeBrain archives on the remote, as rclone lsjson (Name/Size/ModTime).
# The dashboard renders this alongside the local list so the off-site copy is
# visible — and therefore restorable — without a shell.
offsite_list() {
    command -v rclone >/dev/null || { log_warn "rclone is not installed."; return 1; }
    offsite_env || return 1
    rclone lsjson "offsite:${OFFSITE_PATH:-homebrain-backups}" \
        --files-only --max-depth 1 \
        --include '/homebrain_backup*.tar.gz*' \
        --include '/nextcloud_backup*.tar.gz*'
}

# Pull exactly one archive down into $2. $1 is a bare filename, never a path:
# the include pattern is anchored so a crafted value cannot widen this into
# "fetch the entire remote", and the caller (restore.sh) basenames it first.
offsite_fetch() {
    local name="$1" dest="$2"
    command -v rclone >/dev/null || { log_warn "rclone is not installed."; return 1; }
    offsite_env || return 1
    rclone copy "offsite:${OFFSITE_PATH:-homebrain-backups}" "$dest" \
        --max-depth 1 --include "/${name}"
}

# Size in bytes of one remote archive, or empty if it isn't there. Used to
# refuse a fetch that would not fit — filling the root disk of a box the owner
# cannot SSH into is exactly the failure this phase exists to prevent.
offsite_size() {
    local name="$1"
    command -v rclone >/dev/null || return 1
    offsite_env || return 1
    rclone size "offsite:${OFFSITE_PATH:-homebrain-backups}" \
        --max-depth 1 --include "/${name}" --json 2>/dev/null \
        | jq -r 'select(.count > 0) | .bytes' 2>/dev/null
}
