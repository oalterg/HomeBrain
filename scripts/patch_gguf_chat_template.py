#!/usr/bin/env python3
"""
patch_gguf_chat_template.py

Patch a single string inside the tokenizer.chat_template metadata of a GGUF
model file — without loading tensor data into RAM.

Strategy
--------
1. Read the entire metadata section (KV pairs) into memory — typically a few
   MB, never approaching the GB-scale tensor data.
2. Find and replace the target substring in the chat_template value.
3. Rewrite the file as:
       new fixed header  (magic + version + n_tensors + n_kv)
     + new KV section    (all KV pairs, chat_template patched in place)
     + original tensor-info section  (verbatim — offsets are relative to the
                                       tensor-data section, not the file, so
                                       they remain valid)
     + alignment padding  (recalculated for the new header size)
     + tensor data        (streamed verbatim from source, 256 MB at a time)
4. Atomically rename the temp file over the original.

Usage
-----
  python3 patch_gguf_chat_template.py <model.gguf> <old_str> <new_str>

Exit codes: 0 = success, 1 = error, 2 = pattern not found (no-op).
"""

import os
import sys
import struct
import shutil
import tempfile

GGUF_MAGIC = 0x46554747
DEFAULT_ALIGNMENT = 32
CHUNK = 256 * 1024 * 1024  # 256 MiB streaming chunk

# Fixed-width scalar types: {vtype_id: byte_size}
SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


# ---------------------------------------------------------------------------
# Low-level GGUF readers (operate on an in-memory bytes/bytearray)
# ---------------------------------------------------------------------------

def ru32(buf, pos):
    return struct.unpack_from('<I', buf, pos)[0], pos + 4

def ru64(buf, pos):
    return struct.unpack_from('<Q', buf, pos)[0], pos + 8

def rstr(buf, pos):
    """Return (raw_bytes, new_pos) for a GGUF string (u64 len + data)."""
    n, pos = ru64(buf, pos)
    return buf[pos:pos + n], pos + n


def skip_value(buf, pos, vtype):
    """Advance pos past one value of the given type; return new pos."""
    if vtype in SCALAR_SIZES:
        return pos + SCALAR_SIZES[vtype]
    if vtype == 8:  # string
        n, pos = ru64(buf, pos)
        return pos + n
    if vtype == 9:  # array
        et, pos = ru32(buf, pos)
        cnt, pos = ru64(buf, pos)
        for _ in range(cnt):
            pos = skip_value(buf, pos, et)
        return pos
    raise ValueError(f"Unknown GGUF value type {vtype} at offset {pos:#x}")


# ---------------------------------------------------------------------------
# GGUF string encoder
# ---------------------------------------------------------------------------

def enc_str(b: bytes) -> bytes:
    return struct.pack('<Q', len(b)) + b


# ---------------------------------------------------------------------------
# Main patch function
# ---------------------------------------------------------------------------

def patch_gguf(model_path: str, old_str: str, new_str: str) -> int:
    """Return 0 on success, 1 on error, 2 if pattern not found."""
    old_b = old_str.encode('utf-8')
    new_b = new_str.encode('utf-8')

    file_size = os.path.getsize(model_path)
    print(f"[patch_gguf] {model_path}  ({file_size / 1024**3:.2f} GiB)")

    # ------------------------------------------------------------------
    # Step 1: read enough of the file to capture the full metadata section
    # ------------------------------------------------------------------
    read_limit = 128 * 1024 * 1024  # 128 MiB — generous for any GGUF header
    with open(model_path, 'rb') as fh:
        raw = fh.read(read_limit)

    pos = 0
    magic, pos = ru32(raw, pos)
    if magic != GGUF_MAGIC:
        print(f"ERROR: bad magic {magic:#010x}", file=sys.stderr)
        return 1
    version, pos = ru32(raw, pos)
    n_tensors, pos = ru64(raw, pos)
    n_kv, pos = ru64(raw, pos)
    print(f"  GGUF v{version}  tensors={n_tensors}  kv_pairs={n_kv}")

    # ------------------------------------------------------------------
    # Step 2: parse all KV pairs, rebuild metadata with patched template
    # ------------------------------------------------------------------
    new_kv = bytearray()
    found = False
    alignment = DEFAULT_ALIGNMENT

    for _ in range(n_kv):
        kv_start = pos
        key_raw, pos = rstr(raw, pos)
        key = key_raw.decode('utf-8')
        vtype, pos = ru32(raw, pos)

        if key == 'tokenizer.chat_template':
            val_raw, pos = rstr(raw, pos)
            template = val_raw.decode('utf-8')
            if old_b not in val_raw:
                print(f"  Pattern not found in chat_template — nothing to do.", file=sys.stderr)
                return 2
            count = val_raw.count(old_b)
            patched_val = val_raw.replace(old_b, new_b)
            print(f"  Patching chat_template: {count} occurrence(s), "
                  f"Δ{len(patched_val) - len(val_raw):+d} bytes")
            new_kv += enc_str(key_raw)
            new_kv += struct.pack('<I', 8)          # string vtype
            new_kv += enc_str(patched_val)
            found = True

        elif key == 'general.alignment':
            # Preserve raw bytes and remember the alignment value
            val_start = pos
            pos = skip_value(raw, pos, vtype)
            val_bytes = raw[val_start:pos]
            alignment = struct.unpack_from('<I', val_bytes, 0)[0]
            new_kv += enc_str(key_raw) + struct.pack('<I', vtype) + val_bytes

        else:
            val_start = pos
            pos = skip_value(raw, pos, vtype)
            new_kv += raw[kv_start:pos]             # copy entry verbatim

    if not found:
        print("ERROR: tokenizer.chat_template key not found", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Step 3: capture the tensor-info section verbatim (offsets are
    # relative to tensor-data start, not to the file, so they stay valid)
    # ------------------------------------------------------------------
    tensor_info_start = pos
    for _ in range(n_tensors):
        name_raw, pos = rstr(raw, pos)
        n_dims, pos = ru32(raw, pos)
        for _ in range(n_dims):
            _, pos = ru64(raw, pos)
        _ttype, pos = ru32(raw, pos)    # tensor dtype
        _offset, pos = ru64(raw, pos)   # data offset (relative, keep as-is)

    tensor_info_bytes = raw[tensor_info_start:pos]

    # Original tensor-data section start (aligned in the original file)
    orig_td_start = (pos + alignment - 1) // alignment * alignment

    # ------------------------------------------------------------------
    # Step 4: build the new fixed header + KV + tensor-info
    # ------------------------------------------------------------------
    fixed_hdr = struct.pack('<IIQQ', GGUF_MAGIC, version, n_tensors, n_kv)
    new_header = fixed_hdr + bytes(new_kv) + tensor_info_bytes

    # Pad to alignment boundary
    new_td_start = (len(new_header) + alignment - 1) // alignment * alignment
    pad = b'\x00' * (new_td_start - len(new_header))

    tensor_data_size = file_size - orig_td_start
    print(f"  Original tensor-data offset: {orig_td_start:#x}")
    print(f"  New      tensor-data offset: {new_td_start:#x}")
    print(f"  Tensor data to stream:       {tensor_data_size / 1024**3:.2f} GiB")

    # Disk space check
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(model_path))).free
    needed = len(new_header) + len(pad) + tensor_data_size
    if free < needed * 1.05:
        print(f"ERROR: insufficient disk space "
              f"({free / 1024**3:.1f} GiB free, need {needed / 1024**3:.1f} GiB)",
              file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Step 5: write patched file (temp → atomic rename)
    # ------------------------------------------------------------------
    model_dir = os.path.dirname(os.path.abspath(model_path))
    tmp_fd, tmp_path = tempfile.mkstemp(dir=model_dir, suffix='.patching')
    try:
        with os.fdopen(tmp_fd, 'wb') as dst, open(model_path, 'rb') as src:
            dst.write(new_header)
            dst.write(pad)
            src.seek(orig_td_start)
            copied = 0
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                pct = copied / tensor_data_size * 100
                print(f"  Streaming tensor data … {pct:.1f}%", end='\r', flush=True)
        print()  # newline after progress

        # ------------------------------------------------------------------
        # Step 6: quick verification — re-read the patched header
        # ------------------------------------------------------------------
        with open(tmp_path, 'rb') as fh:
            verify_raw = fh.read(read_limit)
        if new_b not in verify_raw:
            raise RuntimeError("Verification failed: new string not in output header")
        if old_b in verify_raw[:new_td_start]:
            raise RuntimeError("Verification failed: old string still present in header")
        print(f"  Verification OK — new string found, old string absent from header")

        # Atomic rename
        os.rename(tmp_path, model_path)
        tmp_path = None  # defuse cleanup
        print(f"  Done: {model_path}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <model.gguf> <old_str> <new_str>")
        sys.exit(1)

    rc = patch_gguf(sys.argv[1], sys.argv[2], sys.argv[3])
    sys.exit(rc)
