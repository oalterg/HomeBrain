"""Hardware regression check: run with ComfyUI's Python and launcher environment.

    python check_comfy_arc.py /home/homebrain/ComfyUI/comfy/ops.py

Loads only the Arc helpers, without starting ComfyUI or monkeypatching torch.
"""
import ast
import sys
from pathlib import Path


def main():
    import torch

    tree = ast.parse(Path(sys.argv[1]).read_text())
    names = {"_xpu_chunked_linear", "_xpu_mm", "_xpu_attention",
             "repeat_kv_for_gqa", "scaled_dot_product_attention"}
    tree.body = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    original = torch.nn.functional.linear
    scope = {"torch": torch, "_orig_linear": original, "_xpu_linear_chunk": {}}
    exec(compile(tree, sys.argv[1], "exec"), scope)
    linear = scope["_xpu_chunked_linear"]
    x = torch.randn(256, 16, device="xpu", dtype=torch.float16)
    w = torch.randn(16, 16, device="xpu", dtype=torch.float16)
    calls = []

    def record(a, b, bias=None):
        calls.append((a.device.type, a.shape[0]))
        return original(a, b, bias)

    scope["_orig_linear"] = record
    result = linear(x, w)
    torch.testing.assert_close(result, original(x, w))
    assert calls == [("xpu", 256)], f"Unnecessary chunking: {calls}"

    # A short prompt's CPU fallback must not force a longer call onto CPU.
    scope["_xpu_linear_chunk"].clear()
    calls.clear()

    def short_fails(a, b, bias=None):
        calls.append((a.device.type, a.shape[0]))
        if a.device.type == "xpu" and a.shape[0] == 1:
            raise RuntimeError("could not create a primitive")
        return original(a, b, bias)

    scope["_orig_linear"] = short_fails
    torch.testing.assert_close(linear(x[:1], w), original(x[:1], w))
    calls.clear()
    torch.testing.assert_close(linear(x, w), original(x, w))
    assert calls == [("xpu", 256)], f"Short-prompt fallback leaked: {calls}"

    # Supported attention must use native SDPA, not the per-head workaround.
    q = torch.randn(1, 2, 8, 16, device="xpu", dtype=torch.float16)
    attention = scope["scaled_dot_product_attention"]

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("Native attention was bypassed")

    fallback = scope["_xpu_attention"]
    scope["_xpu_attention"] = unexpected_fallback
    torch.testing.assert_close(attention(q, q, q),
                               torch.nn.functional.scaled_dot_product_attention(q, q, q))
    scope["_xpu_attention"] = fallback
    native_attention = torch.nn.functional.scaled_dot_product_attention
    expected = native_attention(q, q, q)

    def unsupported(*args, **kwargs):
        raise RuntimeError("could not create a primitive")

    torch.nn.functional.scaled_dot_product_attention = unsupported
    try:
        torch.testing.assert_close(attention(q, q, q), expected, atol=0.005, rtol=0.005)
    finally:
        torch.nn.functional.scaled_dot_product_attention = native_attention
    if len(sys.argv) > 2:
        tree = ast.parse(Path(sys.argv[2]).read_text())
        tree.body = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_matmul_groups"]
        exec(compile(tree, sys.argv[2], "exec"), scope)
        native_matmul = torch.matmul
        devices = []

        def record_matmul(a, b):
            devices.append(a.device.type)
            return native_matmul(a, b)

        grouped = x.reshape(16, 16, 16)
        torch.matmul = record_matmul
        try:
            result = scope["_matmul_groups"](grouped, w)
            torch.testing.assert_close(result, native_matmul(grouped, w))
            assert devices == ["xpu"], f"Rotation forced off GPU: {devices}"
        finally:
            torch.matmul = native_matmul
    torch.xpu.synchronize()
    print("ok: full linear, shape-specific CPU fallback, native attention and fallback")


if __name__ == "__main__":
    main()
