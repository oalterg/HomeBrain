"""Durable generation: retries must never regenerate or change the recipient."""
import os
import sys
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import media_jobs
import picture
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import media_worker
import app as hb
import integrations


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("HB_MEDIA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HB_PICTURES_DIR", str(tmp_path / "pictures"))
    monkeypatch.setenv("HB_PICTURE_STATUS", str(tmp_path / "picture-status.json"))
    monkeypatch.setenv("HB_PICTURE_REQUEST", str(tmp_path / "picture-request.json"))
    monkeypatch.setenv("HOMEBRAIN_HOME", str(tmp_path / "home"))
    return tmp_path


def payload(key="request-1", prompt="A blue teapot"):
    return {"id": "20260928T120000Z-abcd1234", "kind": "picture",
            "model": "krea2", "prompt": prompt, "seed": 7,
            "request_id": key, "source": "mcp", "target": "12345"}


def test_accept_is_idempotent_and_payload_bound(store):
    first = media_jobs.accept(payload())
    assert media_jobs.accept(dict(payload(), seed=99))["id"] == first["id"]
    with pytest.raises(ValueError, match="different"):
        media_jobs.accept(payload(prompt="A red teapot"))
    with pytest.raises(media_jobs.Busy):
        media_jobs.accept(dict(payload("second"), id="20260928T120001Z-abcd1234"))


def test_success_and_pending_delivery_commit_together(store):
    job = media_jobs.accept(payload())
    media_jobs.finish(job["id"], "succeeded", "Picture ready.", chat_ready=False)
    job = media_jobs.get(job["id"])
    assert job["state"] == "succeeded"
    assert job["chat_ready"] == 0
    assert job["delivery"]["state"] == "pending"
    assert not media_jobs.active()
    media_jobs.finish(job["id"], "succeeded", "Picture ready.", chat_ready=False)
    assert len(media_jobs.pending_deliveries()) == 1


def test_qwen_workflow_is_fixed_and_does_not_change_krea_default():
    template = picture.load_template(model="qwen-image-2.1")
    result = picture.build_workflow(template, "A blue teapot", 8, "hbpic/test",
                                    model="qwen-image-2.1")
    assert result["4"]["class_type"] == "TextEncodeQwenImage21"
    assert result["4"]["inputs"]["prompt"] == "A blue teapot"
    assert result["7"]["inputs"]["seed"] == 8
    assert result["7"]["inputs"]["steps"] == 25
    assert picture.load_template()["1"]["inputs"]["unet_name"].startswith("krea2")
    assert picture.model_error("video", "qwen-image-2.1")
    assert picture.model_error("picture", "../../arbitrary")


def test_concurrent_requests_reserve_only_one_job(store):
    def accept(i):
        try:
            return media_jobs.accept(dict(payload(f"request-{i}"), id=f"20260928T120000Z-{i:08x}"))
        except media_jobs.Busy:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(accept, range(4)))
    assert sum(j is not None for j in results) == 1


def test_destination_must_be_explicit_and_paired(store):
    root = store / "home" / ".openclaw"
    (root / "credentials").mkdir(parents=True)
    (root / "openclaw.json").write_text(json.dumps({"channels": {"telegram": {"enabled": True}}}))
    paired = root / "credentials" / "telegram-default-allowFrom.json"
    paired.write_text(json.dumps({"allowFrom": ["12345", "67890", "*", "-100456"]}))
    assert media_jobs.telegram_targets() == ["12345", "67890"]
    assert media_jobs.delivery_target() == ""
    with pytest.raises(ValueError):
        media_jobs.set_delivery_target("55555")
    media_jobs.set_delivery_target("12345")
    assert media_jobs.delivery_target() == "12345"
    paired.write_text('{"allowFrom": ["67890"]}')
    assert media_jobs.delivery_target() == ""


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(hb, "STATUS_FILE", str(store / "task-status.json"))
    monkeypatch.setattr(hb, "read_status", lambda: {"status": "idle"})
    monkeypatch.setattr(hb, "picture_unit_active", lambda kind="picture": False)
    monkeypatch.setattr(hb, "picture_ready", lambda: True)
    monkeypatch.setattr(hb, "video_ready", lambda: True)
    monkeypatch.setattr(picture, "runtime_ready", lambda *a, **kw: True)
    monkeypatch.setattr(media_jobs, "delivery_target", lambda: "12345")
    monkeypatch.setattr(hb, "start_picture_task", lambda p: hb._start_media_task(p, "picture"))
    monkeypatch.setattr(hb, "start_video_task", lambda p: hb._start_media_task(p, "video"))
    monkeypatch.setattr(integrations, "_self_token", lambda: "test-token")
    monkeypatch.setattr(hb.limiter, "enabled", False)
    hb.app.config["TESTING"] = True
    return hb.app.test_client()


def test_api_authentication_and_duplicate_submission(api):
    url = "/api/integrations/self/media/generate"
    body = {"kind": "picture", "prompt": "A teapot", "request_id": "request-1"}
    assert api.post(url, json=body).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    first = api.post(url, json=body, headers=headers)
    assert first.status_code == 202
    assert first.json["model"] == "krea2"
    assert api.post(url, json=body, headers=headers).json["id"] == first.json["id"]
    assert api.post(url, json=dict(body, request_id="request-2"), headers=headers).status_code == 409
    for extra in ({"target": "99999"}, {"workflow": {}}, {"kind": []}, {"model": []}):
        assert api.post(url, json=dict(body, **extra), headers=headers).status_code == 400
    job = api.get('/api/integrations/self/media/jobs/' + first.json['id'], headers=headers)
    assert job.json['state'] == 'accepted'
    assert 'prompt' not in job.json and 'target' not in job.json


def test_dashboard_and_mcp_share_the_reservation(api):
    with api.session_transaction() as s:
        s["authenticated"] = True
    first = api.post('/api/picture', json={'prompt': 'A teapot', 'model': 'qwen-image-2.1'})
    assert first.status_code == 202
    assert not first.json['automatic_delivery']
    assert api.post('/api/video', json={'prompt': 'A moving teapot'}).status_code == 409


def test_worker_survives_restart_and_delivers_without_model(store, monkeypatch):
    job = media_jobs.accept(payload())
    calls = []
    monkeypatch.setattr(media_worker, 'send', lambda j, phase: calls.append(phase) or ('sent', '77', ''))
    monkeypatch.setattr(media_worker, 'unit_state', lambda kind: 'inactive')
    monkeypatch.setattr(media_worker, 'handoff_ready', lambda job: True)
    monkeypatch.setattr(media_worker, 'chat_healthy', lambda: False)
    monkeypatch.setattr(media_worker.subprocess, 'run', lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(picture, 'weights_ready', lambda **kw: True)
    monkeypatch.setattr(media_worker.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024**3))
    media_worker.tick()  # starting notice first; no generation until receipt
    assert calls == ['start']
    media_worker.tick()
    assert media_jobs.active()['state'] == 'running'
    assert len([c for c in calls if isinstance(c, list) and 'homebrain-picture.service' in c and 'start' in c]) == 1
    directory = store / 'pictures'
    (directory / (job['id'] + '.png')).write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0\0\0\rIHDR' + b'\0' * 8)
    (directory / (job['id'] + '.json')).write_text(json.dumps({'id': job['id']}))
    with media_jobs.connect() as db:
        db.execute('UPDATE jobs SET updated=0')
    media_jobs.recover_sends()  # restart: completed generation is not submitted again
    media_worker.tick()
    result = media_jobs.get(job['id'])
    assert result['state'] == 'succeeded'
    assert result['chat_ready'] == 0
    assert result['delivery']['state'] == 'sent'
    assert calls.count('done') == 1
    media_worker.tick()
    assert calls.count('done') == 1


def test_unknown_send_is_not_automatically_retried(store):
    job = media_jobs.accept(payload())
    assert media_jobs.claim_delivery(job['id'], 'start')
    media_jobs.recover_sends()
    assert media_jobs.get(job['id'])['delivery']['state'] == 'unknown'
    assert media_jobs.pending_deliveries() == []
    assert media_jobs.retry_delivery(job['id'])
    assert len(media_jobs.pending_deliveries()) == 1


def test_revocation_stops_delivery_before_any_cli_call(store, monkeypatch):
    monkeypatch.setattr(media_jobs, 'delivery_target', lambda: '')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('sent after revocation'))
    assert media_worker.send(payload(), 'start')[0] == 'blocked'


@pytest.mark.parametrize('output,expected', [
    ('{"payload":{"messageId":42,"chatId":"12345"}}', 'sent'),
    ('plugin loaded\n{"result":{"messageId":"43"}}', 'sent'),
    ('{"ok":false,"error":"429 Too Many Requests"}', 'pending'),
    ('socket timeout', 'unknown'),
])
def test_delivery_receipts(store, monkeypatch, output, expected):
    monkeypatch.setattr(media_jobs, 'delivery_target', lambda: '12345')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=output, stderr='', returncode=0))
    assert media_worker.send(payload(), 'start')[0] == expected


def test_chat_failure_preserves_generated_output_status(store):
    picture.write_status('success', 'Picture ready.', payload()['id'])
    picture.mark_chat(False)
    assert picture.read_status()['state'] == 'success'
    assert picture.read_status()['chat_ready'] is False


def test_artifact_cannot_follow_symlink_outside_output(store):
    directory = store / 'pictures'
    directory.mkdir()
    secret = store / 'secret.png'
    secret.write_bytes(b'\x89PNG\r\n\x1a\n')
    (directory / (payload()['id'] + '.png')).symlink_to(secret)
    assert media_worker.artifact(payload()) is None


def test_gpu_handoff_waits_for_post_tool_inference(monkeypatch):
    monkeypatch.setattr(media_worker, '_idle_job', None)
    monkeypatch.setattr(media_worker, '_idle_since', None)
    now = [0]
    idle = [True]
    monkeypatch.setattr(media_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(media_worker, 'chat_idle', lambda: idle[0])
    assert not media_worker.handoff_ready(payload())
    now[0] = 5
    idle[0] = False  # agent begins its post-tool response
    assert not media_worker.handoff_ready(payload())
    now[0] = 100
    idle[0] = True
    assert not media_worker.handoff_ready(payload())
    now[0] = 111
    assert media_worker.handoff_ready(payload())
