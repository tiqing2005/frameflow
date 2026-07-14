from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from app.models import Asset, Job, Selection
from app.services.common import stream_upload_to_path


TEXT = "人工智能帮助团队提升效率，同时需要可靠的数据安全与可解释素材匹配。"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _ready_project(client, worker, key: str = "invariant-project") -> dict:
    response = client.post(
        "/api/v1/projects/text",
        json={"title": "不变量测试", "text": TEXT},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert worker.run_once() is True
    return client.get(f"/api/v1/projects/{payload['project']['id']}").json()


def test_whitespace_only_values_are_rejected_after_normalization(runtime):
    client, worker, _database, _settings = runtime
    assert client.post(
        "/api/v1/projects/text", json={"title": "   ", "text": TEXT}
    ).status_code == 422
    assert client.post(
        "/api/v1/projects/text", json={"title": "标题", "text": "   "}
    ).status_code == 422

    project = _ready_project(client, worker, "whitespace-project")
    segment = project["segments"][0]
    response = client.patch(
        f"/api/v1/segments/{segment['id']}",
        json={"text": "   ", "version": segment["version"]},
    )
    assert response.status_code == 422
    selected_asset = segment["selection"]["asset_id"]
    response = client.patch(f"/api/v1/assets/{selected_asset}", json={"name": "   "})
    assert response.status_code == 422


def test_disabling_asset_preserves_selection_and_minimum_active_invariants(runtime):
    client, worker, _database, _settings = runtime
    project = _ready_project(client, worker, "asset-in-use-project")
    selected_asset = project["segments"][0]["selection"]["asset_id"]
    conflict = client.patch(f"/api/v1/assets/{selected_asset}", json={"active": False})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASSET_IN_USE"
    assert conflict.json()["details"]["selection_count"] >= 1

    # A fresh project already created selections, so only disable assets that
    # are not currently selected until the business minimum is reached.
    assets = client.get("/api/v1/assets").json()["items"]
    selected_ids = {
        segment["selection"]["asset_id"] for segment in project["segments"]
    }
    candidates = [asset for asset in assets if asset["id"] not in selected_ids]
    active_count = len(assets)
    for asset in candidates:
        if active_count <= 3:
            break
        response = client.patch(f"/api/v1/assets/{asset['id']}", json={"active": False})
        assert response.status_code == 200, response.text
        active_count -= 1
    assert active_count == 3

    final_candidates = client.get("/api/v1/assets").json()["items"]
    unselected = next(asset for asset in final_candidates if asset["id"] not in selected_ids)
    guarded = client.patch(f"/api/v1/assets/{unselected['id']}", json={"active": False})
    assert guarded.status_code == 409
    assert guarded.json()["code"] == "MINIMUM_ASSET_GUARD"
    assert guarded.json()["details"]["minimum_active_assets"] == 3

    worker.heartbeat()
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"]["seed_assets"] == {"ok": True, "count": 3, "minimum": 3}


def test_seed_initialization_preserves_user_editable_fields(runtime):
    client, _worker, database, _settings = runtime
    changed = client.patch(
        "/api/v1/assets/seed-technology",
        json={"name": "用户自定义素材名", "tags": ["自定义标签"], "active": False},
    )
    assert changed.status_code == 200, changed.text

    database.initialize()

    with database.session() as session:
        asset = session.get(Asset, "seed-technology")
        assert asset.name == "用户自定义素材名"
        assert asset.tags_json == '["自定义标签"]'
        assert asset.active is False
        assert asset.is_seed is True


def test_selection_and_deactivation_are_serialized(runtime):
    client, worker, database, _settings = runtime
    project = _ready_project(client, worker, "asset-race-project")
    segment = project["segments"][0]
    current_asset_id = segment["selection"]["asset_id"]
    replacement = next(
        asset for asset in client.get("/api/v1/assets").json()["items"]
        if asset["id"] != current_asset_id
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        deactivate = executor.submit(
            client.patch,
            f"/api/v1/assets/{replacement['id']}",
            json={"active": False},
        )
        select_asset = executor.submit(
            client.put,
            f"/api/v1/segments/{segment['id']}/selection",
            json={"asset_id": replacement["id"]},
        )
        responses = [deactivate.result(), select_asset.result()]

    assert sum(response.status_code == 200 for response in responses) == 1
    with database.session() as session:
        selection = session.scalar(
            select(Selection).where(Selection.segment_id == segment["id"])
        )
        selected_asset = session.get(Asset, selection.asset_id)
        assert selected_asset.active is True


def test_preview_retry_event_uses_preview_stage(runtime):
    client, worker, database, _settings = runtime
    project = _ready_project(client, worker, "preview-retry-project")
    response = client.post(f"/api/v1/projects/{project['project']['id']}/preview", json={})
    assert response.status_code == 202, response.text
    job_id = response.json()["preview"]["job"]["id"]
    with database.session() as session:
        job = session.get(Job, job_id)
        job.status = "failed"
        job.retryable = True
        job.error_code = "PREVIEW_RENDER_FAILED"
        job.error_message = "测试失败"
        job.attempt = 1

    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 202, retried.text
    payload = retried.json()
    assert payload["job"]["stage"] == "preview_planning"
    assert payload["events"][-1]["stage"] == "preview_planning"


def test_upload_streaming_uses_bounded_chunks_and_cleans_oversize(tmp_path: Path):
    class GuardedStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    target = tmp_path / "bounded.bin"
    size, digest, head = stream_upload_to_path(
        GuardedStream(b"a" * (2 * 1024 * 1024 + 17)),
        target,
        3 * 1024 * 1024,
    )
    assert size == target.stat().st_size
    assert len(digest) == 64
    assert head == b"a" * 64

    oversized = tmp_path / "oversized.bin"
    try:
        stream_upload_to_path(GuardedStream(b"b" * 33), oversized, 32, chunk_size=16)
    except Exception as exc:
        assert getattr(exc, "code", None) == "UPLOAD_TOO_LARGE"
    else:
        raise AssertionError("oversized upload should be rejected")
    assert not oversized.exists()
