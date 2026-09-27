"""One frozen Krea 2 picture.

The dashboard accepts a prompt string. It does not accept a Comfy graph.
ComfyUI stays on localhost; this module posts the checked workflow and
copies the PNG out.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request

MAX_PROMPT = 500
ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
PATCH_MARKER = "_xpu_chunked_linear"
COMFY_URL = os.environ.get("HB_COMFY_URL", "http://127.0.0.1:8188")
JOB_TIMEOUT_S = int(os.environ.get("HB_PICTURE_TIMEOUT", "840"))

# Official Comfy-Org Krea 2 Turbo files. The button stays hidden until
# each one is present at this exact size.
WEIGHTS = (
    ("diffusion_models/krea2_turbo_fp8_scaled.safetensors", 13141730784),
    ("text_encoders/qwen3vl_4b_fp8_scaled.safetensors", 5242467968),
    ("vae/qwen_image_vae.safetensors", 253806246),
)

FROZEN_NODES = {
    "1": "UNETLoader",
    "2": "CLIPLoader",
    "3": "VAELoader",
    "4": "CLIPTextEncode",
    "5": "ConditioningZeroOut",
    "6": "EmptyLatentImage",
    "7": "KSampler",
    "8": "VAEDecode",
    "9": "SaveImage",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.normpath(os.path.join(_HERE, os.pardir, "config", "picture", "krea2.json"))


def home() -> str:
    return os.environ.get("HOMEBRAIN_HOME", "/home/homebrain")


def comfy_root() -> str:
    return os.environ.get("HB_COMFY_ROOT", os.path.join(home(), "ComfyUI"))


def pictures_dir() -> str:
    return os.environ.get("HB_PICTURES_DIR", os.path.join(home(), "pictures"))


def request_path() -> str:
    return os.environ.get("HB_PICTURE_REQUEST", "/var/lib/homebrain/picture-request.json")


def status_path() -> str:
    return os.environ.get("HB_PICTURE_STATUS", "/var/lib/homebrain/picture-status.json")


def unit_path() -> str:
    return os.environ.get("HB_PICTURE_UNIT", "/etc/systemd/system/homebrain-picture.service")


def arc_discrete(platform: dict) -> bool:
    """Discrete Arc only. SYCL on xe with a large BAR is that box."""
    return (
        isinstance(platform, dict)
        and platform.get("platform_tag") == "x86_64-sycl"
        and platform.get("gpu_driver") == "xe"
        and platform.get("gpu_backend") == "sycl"
        and platform.get("gpu_memory") == "discrete"
    )


def prompt_error(body) -> str | None:
    """Reject anything that is not a single prompt string."""
    if not isinstance(body, dict) or set(body) != {"prompt"}:
        return "Send a prompt, not a workflow."
    text = body.get("prompt")
    if not isinstance(text, str):
        return "Send a prompt, not a workflow."
    text = text.strip()
    if not text:
        return "Write a prompt first."
    if len(text) > MAX_PROMPT:
        return f"Keep the prompt under {MAX_PROMPT} characters."
    if "\x00" in text:
        return "That prompt cannot be used."
    return None


def load_template(path: str | None = None) -> dict:
    with open(path or TEMPLATE_PATH, encoding="utf-8") as f:
        template = json.load(f)
    if not isinstance(template, dict) or set(template) != set(FROZEN_NODES):
        raise ValueError("picture workflow was modified")
    for node_id, class_type in FROZEN_NODES.items():
        node = template.get(node_id)
        if not isinstance(node, dict) or node.get("class_type") != class_type:
            raise ValueError("picture workflow was modified")
    if template["6"]["inputs"].get("width") != 1024 or template["6"]["inputs"].get("height") != 1024:
        raise ValueError("picture workflow was modified")
    if template["7"]["inputs"].get("steps") != 8:
        raise ValueError("picture workflow was modified")
    return template


def build_workflow(template: dict, prompt: str, seed: int, filename_prefix: str) -> dict:
    """Substitute the prompt and seed. Every other knob stays the checked graph."""
    load_template_shape = set(template) == set(FROZEN_NODES)
    if not load_template_shape:
        raise ValueError("picture workflow was modified")
    workflow = copy.deepcopy(template)
    workflow["4"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["seed"] = int(seed)
    workflow["9"]["inputs"]["filename_prefix"] = filename_prefix
    return workflow


def weights_ready(root: str | None = None) -> bool:
    base = os.path.join(root or comfy_root(), "models")
    for rel, size in WEIGHTS:
        path = os.path.join(base, rel)
        try:
            if os.path.getsize(path) != size:
                return False
        except OSError:
            return False
    return True


def runtime_ready(platform: dict, root: str | None = None, unit: str | None = None) -> bool:
    if not arc_discrete(platform):
        return False
    if not os.path.isfile(unit or unit_path()):
        return False
    ops = os.path.join(root or comfy_root(), "comfy", "ops.py")
    try:
        with open(ops, encoding="utf-8", errors="replace") as f:
            if PATCH_MARKER not in f.read():
                return False
    except OSError:
        return False
    return weights_ready(root)


def _atomic_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_request(payload: dict, path: str | None = None) -> None:
    _atomic_json(path or request_path(), payload)


def write_status(state: str, message: str = "", image_id: str = "", path: str | None = None) -> None:
    current = read_status(path)
    payload = {
        "state": state,
        "message": message,
        "id": image_id or current.get("id") or "",
    }
    _atomic_json(path or status_path(), payload)


def read_status(path: str | None = None) -> dict:
    try:
        with open(path or status_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"state": "idle", "message": "", "id": ""}
    if not isinstance(data, dict):
        return {"state": "idle", "message": "", "id": ""}
    return data


def load_request(path: str | None = None) -> dict:
    with open(path or request_path(), encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("picture request is not a prompt")
    err = prompt_error({"prompt": data.get("prompt")})
    if err:
        raise ValueError(err)
    image_id = data.get("id")
    if not isinstance(image_id, str) or not ID_RE.match(image_id):
        raise ValueError("picture request id is not usable")
    seed = data.get("seed")
    if type(seed) is not int or not (0 <= seed < 2**31):
        raise ValueError("picture request seed is not usable")
    data["prompt"] = data["prompt"].strip()
    return data


def _inside(directory: str, path: str) -> bool:
    root = os.path.realpath(directory)
    return os.path.dirname(os.path.realpath(path)) == root


def list_images(directory: str | None = None) -> list[dict]:
    pictures = directory or pictures_dir()
    if not os.path.isdir(pictures):
        return []
    found = []
    for name in os.listdir(pictures):
        if not name.endswith(".json"):
            continue
        image_id = name[:-5]
        if not ID_RE.match(image_id):
            continue
        png = os.path.join(pictures, image_id + ".png")
        meta_path = os.path.join(pictures, name)
        if not _inside(pictures, png) or not os.path.isfile(png):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        found.append({
            "id": image_id,
            "prompt": meta.get("prompt") if isinstance(meta.get("prompt"), str) else "",
            "seed": meta.get("seed") if type(meta.get("seed")) is int else None,
            "created": meta.get("created") if isinstance(meta.get("created"), str) else "",
        })
    found.sort(key=lambda row: row["created"], reverse=True)
    return found[:24]


def image_file(image_id: str, directory: str | None = None) -> str | None:
    if not ID_RE.match(image_id or ""):
        return None
    pictures = directory or pictures_dir()
    path = os.path.join(pictures, image_id + ".png")
    if not _inside(pictures, path) or not os.path.isfile(path):
        return None
    return path


def delete_image(image_id: str, directory: str | None = None) -> bool:
    path = image_file(image_id, directory)
    if path is None:
        return False
    pictures = directory or pictures_dir()
    os.remove(path)
    meta = os.path.join(pictures, image_id + ".json")
    if _inside(pictures, meta) and os.path.isfile(meta):
        os.remove(meta)
    return True


def _http_json(method: str, url: str, payload: dict | None = None, timeout: int = 60):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    return json.loads(raw)


def _error_text(status: dict) -> str:
    for item in status.get("messages") or []:
        if isinstance(item, list) and len(item) == 2 and item[0] == "execution_error":
            detail = item[1] if isinstance(item[1], dict) else {}
            message = detail.get("exception_message") or detail.get("exception_type") or ""
            node = detail.get("node_type") or ""
            text = f"{node}: {message}".strip(": ")
            if text:
                return text[:400]
    return "ComfyUI did not finish the picture."


def execute_job(timeout_s: int = JOB_TIMEOUT_S) -> None:
    """Post the frozen workflow to a ComfyUI that is already listening."""
    request = load_request()
    template = load_template()
    workflow = build_workflow(
        template, request["prompt"], request["seed"], f"hbpic/{request['id']}"
    )
    try:
        queued = _http_json("POST", f"{COMFY_URL}/prompt", {"prompt": workflow}, timeout=120)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        write_status("error", body or "ComfyUI rejected the picture.", request["id"])
        raise SystemExit(1) from exc
    prompt_id = queued.get("prompt_id")
    if not prompt_id or queued.get("node_errors"):
        write_status("error", "ComfyUI rejected the picture.", request["id"])
        raise SystemExit(1)

    deadline = time.time() + timeout_s
    item = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            hist = _http_json("GET", f"{COMFY_URL}/history/{prompt_id}", timeout=30)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        item = hist.get(prompt_id)
        if not item:
            continue
        status = item.get("status") or {}
        if status.get("status_str") == "error":
            write_status("error", _error_text(status), request["id"])
            raise SystemExit(1)
        if status.get("completed"):
            break
    else:
        write_status("error", "The picture took too long and was stopped.", request["id"])
        raise SystemExit(1)

    output_dir = os.path.join(comfy_root(), "output", "hbpic")
    matches = glob.glob(os.path.join(output_dir, request["id"] + "_*.png"))
    matches = [path for path in matches if os.path.isfile(path)]
    if not matches:
        write_status("error", "ComfyUI finished without an image.", request["id"])
        raise SystemExit(1)
    src = max(matches, key=os.path.getmtime)
    os.makedirs(pictures_dir(), exist_ok=True)
    dest = os.path.join(pictures_dir(), request["id"] + ".png")
    shutil.copy2(src, dest)
    os.chmod(dest, 0o640)
    _give(dest)
    sidecar = {
        "id": request["id"],
        "prompt": request["prompt"],
        "seed": request["seed"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta = os.path.join(pictures_dir(), request["id"] + ".json")
    _atomic_json(meta, sidecar)
    os.chmod(meta, 0o640)
    _give(meta)
    write_status("success", "Picture ready.", request["id"])


def _give(path: str) -> None:
    """Hand the file to the homebrain user when that account exists."""
    try:
        import pwd
        ent = pwd.getpwnam("homebrain")
    except KeyError:
        return
    try:
        os.chown(path, ent.pw_uid, ent.pw_gid)
    except OSError:
        pass


def settle() -> None:
    """A killed job must not stay 'running' or the button spins forever."""
    current = read_status()
    if current.get("state") == "running":
        write_status("error", "The picture was interrupted.", current.get("id") or "")


def mark_chat(ok: bool) -> None:
    """Record that llama-server did not become healthy again."""
    if ok:
        return
    current = read_status()
    if current.get("state") == "success":
        write_status(
            "error",
            "The picture was saved, but chat did not come back.",
            current.get("id") or "",
        )
    else:
        message = (current.get("message") or "The picture failed.").rstrip(".")
        write_status("error", message + ". Chat did not come back.", current.get("id") or "")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    cmd = argv[1]
    if cmd == "weights":
        return 0 if weights_ready() else 1
    if cmd == "check":
        try:
            load_request()
            load_template()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(exc)
            return 1
        if not weights_ready():
            print("Krea 2 weights are missing.")
            return 1
        return 0
    if cmd == "status":
        state = argv[2] if len(argv) > 2 else "running"
        message = argv[3] if len(argv) > 3 else ""
        image_id = ""
        try:
            image_id = load_request().get("id") or ""
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        write_status(state, message, image_id)
        return 0
    if cmd == "execute":
        try:
            execute_job()
        except SystemExit as exc:
            return int(exc.code or 1)
        return 0
    if cmd == "settle":
        settle()
        return 0
    if cmd == "mark-chat":
        mark_chat(argv[2] == "ok" if len(argv) > 2 else False)
        return 0
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
