#!/bin/bash
# Run gpu_keepalive.py as llama-server's user, in llama-server's SYCL
# environment, so it reaches the GPU through the same driver.
#
# Only while llama-server runs. A stopped server means a picture or video job
# owns the GPU, and a recovery in progress must not find the card busy.
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
[[ "$(systemctl show -p ActiveState --value llama-server 2>/dev/null)" == active ]] || exit 0
for unit in homebrain-gpu-recover.service homebrain-gpu-wedged.service; do
    [[ "$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)" == activating ]] && exit 0
done
exec_start=$(systemctl show -p ExecStart --value llama-server)
# A GPU that is answering is not idle. Running alongside is safe, but there
# is nothing to gain from it.
port=$(grep -o -- '--port [0-9]*' <<<"$exec_start" | head -1 | cut -d' ' -f2)
if curl -sf -m 2 "http://127.0.0.1:${port:-8001}/slots" 2>/dev/null | grep -q '"is_processing":true'; then
    exit 0
fi
exec_path=$(grep -o 'path=[^ ;]*' <<<"$exec_start" | head -1 | cut -d= -f2)
env_sh="$(dirname "$exec_path")/sycl-env.sh"
# Only SYCL boxes have it, and only they need this.
[[ -x "$env_sh" ]] || exit 0
user=$(systemctl show -p User --value llama-server)
user=${user:-homebrain}
# setpriv, not runuser: no PAM session, so no two journal lines per run.
home=$(getent passwd "$user" | cut -d: -f6)
exec env HOME="${home:-/}" setpriv --reuid="$user" --regid="$user" --init-groups -- \
    "$env_sh" /usr/bin/python3 "$here/gpu_keepalive.py"
