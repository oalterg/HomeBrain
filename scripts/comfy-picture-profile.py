"""Temporary ComfyUI custom node for profiling the frozen picture workflow.

Copy into ComfyUI/custom_nodes, run one picture, then remove it. Results are
JSON lines prefixed HB_PICTURE_PROFILE in picture-comfy.log. Node seconds
include GPU completion and transfers; cpu_seconds measures CPU kernels only.
No prompt text or tensor contents are logged. Use only for isolated benchmarks.
"""
import functools
import json
import logging
import time
from collections import defaultdict

import torch
import comfy.ops
import execution

NODE_CLASS_MAPPINGS = {}
_stats = defaultdict(lambda: {"calls": 0, "failures": 0, "cpu_seconds": 0.0})


def _measure(name, operation):
    @functools.wraps(operation)
    def measured(a, b, *args, **kwargs):
        key = (name, str(a.device), str(a.dtype), tuple(a.shape), tuple(b.shape))
        stat = _stats[key]
        stat["calls"] += 1
        cpu = a.device.type == "cpu"
        started = time.perf_counter() if cpu else None
        try:
            return operation(a, b, *args, **kwargs)
        except RuntimeError:
            stat["failures"] += 1
            raise
        finally:
            if cpu:
                stat["cpu_seconds"] += time.perf_counter() - started
    return measured


# The Arc patch calls this saved function for both GPU attempts and CPU
# fallbacks. Wrapping F.linear instead would miss its internal fallback calls.
comfy.ops._orig_linear = _measure("linear", comfy.ops._orig_linear)
torch.matmul = _measure("matmul", torch.matmul)
_execute = execution.execute


@functools.wraps(_execute)
async def _profile_execute(server, dynprompt, caches, current_item, *args, **kwargs):
    torch.xpu.synchronize()
    _stats.clear()
    started = time.perf_counter()
    try:
        return await _execute(server, dynprompt, caches, current_item, *args, **kwargs)
    finally:
        torch.xpu.synchronize()
        seconds = time.perf_counter() - started
        operations = [
            dict(operation=name, device=device, dtype=dtype, input_shape=a,
                 weight_shape=b, **stat)
            for (name, device, dtype, a, b), stat in _stats.items()
        ]
        logging.info("HB_PICTURE_PROFILE %s", json.dumps({
            "node": current_item,
            "class_type": dynprompt.get_node(current_item)["class_type"],
            "seconds": seconds,
            "operations": operations,
        }))


execution.execute = _profile_execute
