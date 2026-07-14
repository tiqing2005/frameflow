from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Job
from app.preview import PreviewRenderTimeout, render_preview


TEXT = "人工智能正在提升团队协作效率。数据安全帮助企业保护用户隐私。"


def _ready_project(client, worker):
    response = client.post(
        "/api/v1/projects/text",
        json={"title": "预览视频测试", "text": TEXT},
        headers={"Idempotency-Key": "preview-project"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert worker.run_once() is True
    return payload["project"]["id"]


def test_timeline_and_preview_render_are_durable_and_idempotent(runtime, monkeypatch):
    client, worker, _database, _settings = runtime
    project_id = _ready_project(client, worker)

    timeline = client.get(f"/api/v1/projects/{project_id}/timeline")
    assert timeline.status_code == 200, timeline.text
    plan = timeline.json()
    assert plan["segment_count"] >= 1
    assert plan["duration_ms"] > 0
    assert all(item["asset"]["file_url"] for item in plan["items"])

    created = client.post(f"/api/v1/projects/{project_id}/preview", json={})
    assert created.status_code == 202, created.text
    queued = created.json()
    assert queued["preview"]["status"] == "queued"
    assert queued["preview"]["job"]["kind"] == "preview"
    assert queued["idempotent_replay"] is False

    def fake_render(plan, output_path: Path, **_kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-mp4-preview")
        return {
            "output_path": str(output_path),
            "size_bytes": output_path.stat().st_size,
            "duration_ms": plan["duration_ms"],
            "segment_count": plan["segment_count"],
            "subtitles_burned": True,
            "codec": "h264",
            "resolution": "1280x720",
            "fps": 25,
        }

    monkeypatch.setattr("app.worker.render_preview", fake_render)
    assert worker.run_once() is True

    detail = client.get(f"/api/v1/projects/{project_id}/preview")
    assert detail.status_code == 200
    preview = detail.json()["preview"]
    assert preview["status"] == "succeeded"
    assert preview["output_url"].startswith(f"/media/previews/{project_id}/")
    assert preview["job"]["status"] == "succeeded"

    replay = client.post(f"/api/v1/projects/{project_id}/preview", json={})
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["preview"]["id"] == preview["id"]

    runs = client.get("/api/v1/runs").json()["items"]
    render_run = next(item for item in runs if item["operation"] == "preview_render")
    assert render_run["provider"] == "ffmpeg"
    assert render_run["model"] == "h264-storyboard-v1"
    assert render_run["input_hash"] == preview["input_hash"]


def test_preview_rejects_non_ready_project(runtime):
    client, _worker, _database, _settings = runtime
    response = client.post(
        "/api/v1/projects/text",
        json={"title": "尚未处理", "text": TEXT},
        headers={"Idempotency-Key": "preview-not-ready"},
    )
    project_id = response.json()["project"]["id"]
    preview = client.post(f"/api/v1/projects/{project_id}/preview", json={})
    assert preview.status_code == 409
    assert preview.json()["code"] == "PROJECT_NOT_READY"


def test_force_does_not_orphan_an_active_preview_job(runtime):
    client, worker, database, _settings = runtime
    project_id = _ready_project(client, worker)

    first = client.post(f"/api/v1/projects/{project_id}/preview", json={})
    assert first.status_code == 202
    first_payload = first.json()
    second = client.post(f"/api/v1/projects/{project_id}/preview", json={"force": True})
    assert second.status_code == 202
    second_payload = second.json()

    assert second_payload["idempotent_replay"] is True
    assert second_payload["preview"]["job_id"] == first_payload["preview"]["job_id"]
    with database.session() as session:
        preview_jobs = session.scalar(
            select(func.count()).select_from(Job).where(
                Job.project_id == project_id,
                Job.kind == "preview",
            )
        )
        assert preview_jobs == 1


def test_subtitle_timeout_is_not_reported_as_success(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    output = tmp_path / "preview.mp4"
    plan = {
        "duration_ms": 2_000,
        "segment_count": 1,
        "items": [
            {
                "storage_path": str(source),
                "duration_ms": 2_000,
                "start_ms": 0,
                "end_ms": 2_000,
                "text": "字幕",
                "asset": {"kind": "image"},
            }
        ],
    }
    calls = 0

    def fake_run(command, *, cwd, deadline):
        nonlocal calls
        calls += 1
        if calls == 1:
            (cwd / "clip-000.mp4").write_bytes(b"clip")
        elif calls == 2:
            (cwd / "joined.mp4").write_bytes(b"joined")
        else:
            raise PreviewRenderTimeout("预览渲染超过允许时间")

    monkeypatch.setattr("app.preview._video_encoder", lambda *_args: ("test", []))
    monkeypatch.setattr("app.preview._run", fake_run)
    with pytest.raises(PreviewRenderTimeout):
        render_preview(plan, output, timeout=1)
    assert not output.exists()


def test_encoder_probe_never_exceeds_render_deadline(tmp_path, monkeypatch):
    observed = {}

    class Completed:
        stdout = " V..... libx264 encoder"
        stderr = ""

    def fake_run(*_args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        return Completed()

    monkeypatch.setattr("app.preview.subprocess.run", fake_run)
    monkeypatch.setattr("app.preview._ffmpeg_executable", lambda: "ffmpeg")
    from app.preview import _video_encoder

    deadline = time.monotonic() + 0.2
    encoder, _args = _video_encoder(tmp_path, deadline)
    assert encoder == "libx264"
    assert 0 < observed["timeout"] <= 0.2
