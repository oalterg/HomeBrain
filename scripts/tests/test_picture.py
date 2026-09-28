#!/usr/bin/env python3
"""The picture button accepts a prompt and refuses a Comfy graph.

    python3 scripts/tests/test_picture.py
    pytest scripts/tests/test_picture.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402
import picture              # noqa: E402


ARC = {
    "platform_tag": "x86_64-sycl",
    "gpu_driver": "xe",
    "gpu_backend": "sycl",
    "gpu_memory": "discrete",
    "has_gpu": True,
}
AMD = {
    "platform_tag": "x86_64-vulkan",
    "gpu_driver": "amdgpu",
    "gpu_backend": "vulkan",
    "gpu_memory": "discrete",
    "has_gpu": True,
}


def test_only_a_discrete_arc_qualifies():
    assert picture.arc_discrete(ARC)
    assert not picture.arc_discrete(AMD)
    integrated = dict(ARC, gpu_memory="unified", gpu_backend="vulkan", platform_tag="x86_64-vulkan")
    assert not picture.arc_discrete(integrated)
    assert not picture.arc_discrete({})


def test_a_graph_is_rejected():
    assert picture.prompt_error({"class_type": "KSampler", "inputs": {}})
    assert picture.prompt_error({"prompt": {"class_type": "KSampler"}})
    assert picture.prompt_error({"prompt": "a cup", "workflow": {}})
    assert picture.prompt_error({"prompt": ""})
    assert picture.prompt_error({"prompt": "x" * 501})
    assert picture.prompt_error({"prompt": "a blue cup"}) is None


def test_workflow_only_substitutes_the_prompt_and_seed():
    template = picture.load_template()
    built = picture.build_workflow(template, "a blue enamel cup", 7, "hbpic/20260927T120000Z-abcd1234")
    assert set(built) == set(picture.FROZEN_NODES)
    for node_id, class_type in picture.FROZEN_NODES.items():
        assert built[node_id]["class_type"] == class_type
    assert built["4"]["inputs"]["text"] == "a blue enamel cup"
    assert built["7"]["inputs"]["seed"] == 7
    assert built["7"]["inputs"]["steps"] == 8
    assert built["7"]["inputs"]["cfg"] == 1.0
    assert built["6"]["inputs"]["width"] == 1024
    assert built["6"]["inputs"]["height"] == 1024
    assert built["9"]["inputs"]["filename_prefix"] == "hbpic/20260927T120000Z-abcd1234"
    assert built["1"]["inputs"]["unet_name"] == "krea2_turbo_fp8_scaled.safetensors"


def test_weights_must_match_the_published_sizes(tmp_path=None):
    import tempfile
    directory = tempfile.mkdtemp()
    saved = picture.WEIGHTS
    try:
        picture.WEIGHTS = (("diffusion_models/a.safetensors", 4),)
        path = os.path.join(directory, "models", "diffusion_models")
        os.makedirs(path)
        target = os.path.join(path, "a.safetensors")
        with open(target, "wb") as f:
            f.write(b"no")
        assert not picture.weights_ready(directory)
        with open(target, "wb") as f:
            f.write(b"1234")
        assert picture.weights_ready(directory)
    finally:
        picture.WEIGHTS = saved


def test_runtime_requires_the_unit_the_patch_and_the_weights():
    import tempfile
    directory = tempfile.mkdtemp()
    unit = os.path.join(directory, "homebrain-picture.service")
    ops = os.path.join(directory, "comfy")
    os.makedirs(ops)
    with open(os.path.join(ops, "ops.py"), "w", encoding="utf-8") as f:
        f.write("def _xpu_chunked_linear():\n    pass\n")
    with open(unit, "w", encoding="utf-8") as f:
        f.write("[Service]\nType=oneshot\n")
    saved = picture.WEIGHTS
    try:
        picture.WEIGHTS = ()
        assert picture.runtime_ready(ARC, directory, unit)
        assert not picture.runtime_ready(AMD, directory, unit)
        os.remove(unit)
        assert not picture.runtime_ready(ARC, directory, unit)
    finally:
        picture.WEIGHTS = saved


def test_image_ids_cannot_escape_the_pictures_directory():
    import tempfile
    directory = tempfile.mkdtemp()
    assert picture.image_file("../secret", directory) is None
    assert picture.image_file("20260927T120000Z-abcd1234", directory) is None
    image_id = "20260927T120000Z-abcd1234"
    png = os.path.join(directory, image_id + ".png")
    with open(png, "wb") as f:
        f.write(b"png")
    with open(os.path.join(directory, image_id + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": image_id, "prompt": "a cup", "seed": 3, "created": "2026-09-27T12:00:00Z"}, f)
    assert os.path.realpath(picture.image_file(image_id, directory)) == os.path.realpath(png)
    listed = picture.list_images(directory)
    assert listed[0]["prompt"] == "a cup"
    assert picture.delete_image(image_id, directory)
    assert picture.list_images(directory) == []


def _client():
    hb.app.config["TESTING"] = True
    hb.limiter.enabled = False
    client = hb.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def test_the_route_rejects_a_graph_before_starting_a_job():
    started = []
    hb.start_picture_task = lambda payload: started.append(payload) or True
    client = _client()
    graph = client.post("/api/picture", json={"prompt": {"1": {"class_type": "KSampler"}}})
    extra = client.post("/api/picture", json={"prompt": "a cup", "class_type": "KSampler"})
    assert graph.status_code == 400, graph.status_code
    assert extra.status_code == 400
    assert started == []


def test_a_prompt_on_a_box_without_the_runtime_does_not_start():
    hb.picture_ready = lambda: False
    hb.start_picture_task = lambda payload: (_ for _ in ()).throw(AssertionError("started"))
    client = _client()
    response = client.post("/api/picture", json={"prompt": "a blue cup"})
    assert response.status_code == 404


def test_a_ready_box_records_the_prompt_and_starts_once():
    seen = []

    def start(payload):
        seen.append(payload)
        return dict(payload, state="accepted")

    hb.picture_ready = lambda: True
    hb.start_picture_task = start
    client = _client()
    response = client.post("/api/picture", json={"prompt": "  a blue enamel cup  "})
    assert response.status_code == 202, response.get_json()
    assert seen[0]["prompt"] == "a blue enamel cup"
    assert type(seen[0]["seed"]) is int
    assert picture.ID_RE.match(seen[0]["id"])
    def busy_start(payload):
        raise hb.media_jobs.Busy()
    hb.start_picture_task = busy_start
    busy = client.post("/api/picture", json={"prompt": "another"})
    assert busy.status_code == 409


def test_an_interrupted_job_does_not_stay_running(tmp_path=None):
    import tempfile
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "status.json")
    os.environ["HB_PICTURE_STATUS"] = path
    try:
        picture.write_status("running", "Chat is paused while the picture is made.", "20260927T160624Z-38255839", path)
        picture.settle()
        assert picture.read_status(path)["state"] == "error"
        picture.write_status("success", "Picture ready.", "20260927T160624Z-38255839", path)
        picture.settle()
        assert picture.read_status(path)["state"] == "success"
    finally:
        os.environ.pop("HB_PICTURE_STATUS", None)


def test_service_is_oneshot_and_does_not_start_at_boot():
    path = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "config", "homebrain-picture.service")
    text = open(path, encoding="utf-8").read()
    assert "Type=oneshot" in text
    assert "[Install]" not in text
    assert "picture.sh run" in text


if __name__ == "__main__":
    test_only_a_discrete_arc_qualifies()
    test_a_graph_is_rejected()
    test_workflow_only_substitutes_the_prompt_and_seed()
    test_weights_must_match_the_published_sizes()
    test_runtime_requires_the_unit_the_patch_and_the_weights()
    test_image_ids_cannot_escape_the_pictures_directory()
    test_the_route_rejects_a_graph_before_starting_a_job()
    test_a_prompt_on_a_box_without_the_runtime_does_not_start()
    test_a_ready_box_records_the_prompt_and_starts_once()
    test_an_interrupted_job_does_not_stay_running()
    test_service_is_oneshot_and_does_not_start_at_boot()
    print("ok")
