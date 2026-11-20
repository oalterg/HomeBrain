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
exec_path=$(systemctl show -p ExecStart --value llama-server | grep -o 'path=[^ ;]*' | head -1 | cut -d= -f2)
env_sh="$(dirname "$exec_path")/sycl-env.sh"
# Only SYCL boxes have it, and only they need this.
[[ -x "$env_sh" ]] || exit 0
user=$(systemctl show -p User --value llama-server)
exec runuser -u "${user:-homebrain}" -- "$env_sh" /usr/bin/python3 "$here/gpu_keepalive.py"
