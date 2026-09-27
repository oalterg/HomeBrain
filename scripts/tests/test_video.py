"""Frozen local video jobs share the picture runner and its exclusive task slot."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb
import picture


def client():
    hb.app.config["TESTING"] = True
    hb.limiter.enabled = False
    result = hb.app.test_client()
    with result.session_transaction() as session:
        session["authenticated"] = True
    return result


def test_video_workflow_changes_only_prompt_seed_and_output():
    template = picture.load_template(kind="video")
    result = picture.build_workflow(template, "A teapot steaming", 7, "hbvideo/test", kind="video")
    expected = json.loads(json.dumps(template))
    expected["6"]["inputs"]["prompt"] = "A teapot steaming"
    expected["10"]["inputs"]["noise_seed"] = 7
    expected["15"]["inputs"]["filename_prefix"] = "hbvideo/test"
    assert result == expected
    assert template["6"]["inputs"]["prompt"] == ""
    assert result["6"]["inputs"]["length"] == 56
    assert result["8"]["inputs"]["steps"] == 8
    assert result["15"]["inputs"]["format"] == "mp4"
    assert result["15"]["inputs"]["format.codec"] == "h264"


def test_video_rejects_client_workflows(monkeypatch):
    monkeypatch.setattr(hb, "start_video_task", lambda _: (_ for _ in ()).throw(AssertionError("started")))
    response = client().post("/api/video", json={"prompt": "a cup", "workflow": {}})
    assert response.status_code == 400


def test_video_requires_its_own_weights(monkeypatch, tmp_path):
    monkeypatch.setattr(picture, "WEIGHTS", ())
    assert picture.weights_ready(str(tmp_path))
    assert not picture.weights_ready(str(tmp_path), kind="video")
    monkeypatch.setattr(hb, "video_ready", lambda: False)
    assert client().post("/api/video", json={"prompt": "a cup"}).status_code == 404


def test_video_uses_exclusive_job_slot(monkeypatch):
    monkeypatch.setattr(hb, "video_ready", lambda: True)
    monkeypatch.setattr(hb, "start_video_task", lambda _: False)
    assert client().post("/api/video", json={"prompt": "a cup"}).status_code == 409


def test_video_records_prompt_and_seed(monkeypatch):
    seen = []
    monkeypatch.setattr(hb, "video_ready", lambda: True)
    monkeypatch.setattr(hb, "start_video_task", lambda payload: seen.append(payload) or True)
    assert client().post("/api/video", json={"prompt": "  a cup  "}).status_code == 200
    assert seen[0]["prompt"] == "a cup"
    assert type(seen[0]["seed"]) is int


def test_video_files_are_isolated_and_cannot_escape(tmp_path):
    image_id = "20260927T180000Z-be000001"
    (tmp_path / (image_id + ".mp4")).write_bytes(b"video")
    (tmp_path / (image_id + ".json")).write_text(json.dumps({"prompt": "a cup"}))
    assert picture.image_file(image_id, str(tmp_path)) is None
    assert picture.image_file("../bad", str(tmp_path), kind="video") is None
    assert picture.list_images(str(tmp_path), kind="video")[0]["prompt"] == "a cup"
    assert picture.delete_image(image_id, str(tmp_path), kind="video")
    assert picture.list_images(str(tmp_path), kind="video") == []


def test_running_video_keeps_task_slot_after_manager_restart(monkeypatch):
    monkeypatch.setattr(hb, "read_status", lambda: {"status": "idle"})
    monkeypatch.setattr(hb, "picture_unit_active", lambda kind="picture": kind == "video")
    assert hb.task_running()
