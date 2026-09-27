#!/bin/bash
# ComfyUI on a discrete Arc. Do not source setvars.sh: the oneAPI 2025.3
# libs break torch 2.14.0+xpu (libsycl.so.9 / urDeviceWaitExp).
set +u
export LD_LIBRARY_PATH="/home/homebrain/ComfyUI/.venv/lib:/opt/intel/neo-26.35/usr/local/lib:/opt/intel/neo-26.35/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# oneDNN's SYCL kernels also need the matching GPU OpenCL driver. The torch
# venv's ICD loader otherwise finds only the system's CPU OpenCL runtime.
export OCL_ICD_FILENAMES=/opt/intel/neo-26.35/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so
export OverrideCsrAllocationSize=1048576
export NEOReadDebugKeys=1
export ONEAPI_DEVICE_SELECTOR=level_zero:0
cd /home/homebrain/ComfyUI
exec /home/homebrain/ComfyUI/.venv/bin/python main.py --listen 127.0.0.1 --port 8188 "$@"
