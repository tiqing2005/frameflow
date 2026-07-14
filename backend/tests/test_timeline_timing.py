from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, inspect, select

from app.config import Settings
from app.db import Database
from app.models import AIRun, AuditEvent, PreviewRender, utcnow
from app.services.previews import allocate_timeline_durations


TEXT = (
    "人工智能正在帮助团队提升工作效率。"
    "远程办公让协作方式更加灵活。"
    "数据安全仍然需要持续关注。"
)


def _ready_project(client, worker, key: str = "timeline-timing") -> dict:
    response = client.post(
        "/api/v1/projects/text",
        json={"title": "节奏调整测试", "text": TEXT},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    assert worker.run_once() is True
    return client.get(f"/api/v1/projects/{response.json()['project']['id']}").json()


def test_segment_timing_updates_without_rematch_and_marks_preview_stale(runtime):
    client, worker, database, _settings = runtime
    project = _ready_project(client, worker)
    project_id = project["project"]["id"]
    segment = project["segments"][0]
    original_timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    queued = client.post(f"/api/v1/projects/{project_id}/preview", json={}).json()["preview"]
    requested_duration = 3_333
    if original_timeline["items"][0]["duration_ms"] == requested_duration:
        requested_duration = 3_334

    with database.session() as session:
        rematches_before = session.scalar(
            select(func.count()).select_from(AIRun).where(AIRun.operation == "segment_rematch")
        )
    response = client.patch(
        f"/api/v1/segments/{segment['id']}/timing",
        json={"duration_ms": requested_duration, "version": segment["version"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    expected_duration = ((requested_duration + 20) // 40) * 40
    assert payload["segment"]["render_duration_ms"] == expected_duration
    assert payload["segment"]["version"] == segment["version"] + 1
    item = next(
        item for item in payload["timeline"]["items"] if item["segment_id"] == segment["id"]
    )
    assert item["duration_ms"] == expected_duration
    assert item["effective_duration_ms"] == expected_duration
    assert item["duration_ms"] % 40 == 0
    assert item["duration_source"] == "manual"
    assert payload["timeline"]["input_hash"] != original_timeline["input_hash"]

    preview = client.get(f"/api/v1/projects/{project_id}/preview").json()
    assert preview["preview"]["id"] == queued["id"]
    assert preview["preview"]["input_hash"] != preview["timeline"]["input_hash"]
    with database.session() as session:
        rematches_after = session.scalar(
            select(func.count()).select_from(AIRun).where(AIRun.operation == "segment_rematch")
        )
        audit = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "segment.timing_updated")
            .order_by(AuditEvent.created_at.desc())
        )
    assert rematches_after == rematches_before
    assert audit is not None

    stale = client.patch(
        f"/api/v1/segments/{segment['id']}/timing",
        json={"duration_ms": 4_000, "version": segment["version"]},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "SEGMENT_VERSION_CONFLICT"

    restored = client.patch(
        f"/api/v1/segments/{segment['id']}/timing",
        json={"duration_ms": None, "version": payload["segment"]["version"]},
    )
    assert restored.status_code == 200, restored.text
    restored_item = next(
        item for item in restored.json()["timeline"]["items"] if item["segment_id"] == segment["id"]
    )
    assert restored.json()["segment"]["render_duration_ms"] is None
    assert restored_item["duration_source"] == "auto"
    assert restored_item["duration_ms"] == restored_item["auto_duration_ms"]


def test_fit_timeline_is_exact_bounded_atomic_and_restorable(runtime):
    client, worker, database, _settings = runtime
    project = _ready_project(client, worker, "timeline-fit")
    project_id = project["project"]["id"]
    original = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    target = original["segment_count"] * 4_123
    normalized_target = ((target + 20) // 40) * 40

    fitted = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": target,
            "strategy": "text",
            "expected_input_hash": original["input_hash"],
        },
    )
    assert fitted.status_code == 200, fitted.text
    timeline = fitted.json()
    assert timeline["duration_ms"] == normalized_target
    assert sum(item["duration_ms"] for item in timeline["items"]) == normalized_target
    assert all(1_000 <= item["duration_ms"] <= 30_000 for item in timeline["items"])
    assert all(item["duration_ms"] % 40 == 0 for item in timeline["items"])
    assert all(item["duration_source"] == "manual" for item in timeline["items"])

    conflict = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": target + 1_000,
            "strategy": "equal",
            "expected_input_hash": original["input_hash"],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "TIMELINE_INPUT_CONFLICT"
    current = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    assert current["input_hash"] == timeline["input_hash"]

    restored = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "restore_auto",
            "strategy": "current",
            "expected_input_hash": timeline["input_hash"],
        },
    )
    assert restored.status_code == 200, restored.text
    restored_timeline = restored.json()
    assert all(item["render_duration_ms"] is None for item in restored_timeline["items"])
    assert all(item["duration_source"] == "auto" for item in restored_timeline["items"])
    with database.session() as session:
        actions = set(
            session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.action.in_(
                        {"timeline.timing_fitted", "timeline.timing_restored_auto"}
                    )
                )
            ).all()
        )
    assert actions == {"timeline.timing_fitted", "timeline.timing_restored_auto"}


def test_timeline_timing_limits_and_concurrent_hash_fencing(runtime):
    client, worker, _database, settings = runtime
    project = _ready_project(client, worker, "timeline-limits")
    project_id = project["project"]["id"]
    timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    count = timeline["segment_count"]
    assert timeline["limits"] == {
        "segment_min_duration_ms": 1_000,
        "segment_max_duration_ms": 30_000,
        "timeline_max_duration_ms": settings.preview_max_seconds * 1_000,
        "frame_duration_ms": 40,
    }

    infeasible = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": count * 1_000 - 40,
            "strategy": "equal",
            "expected_input_hash": timeline["input_hash"],
        },
    )
    assert infeasible.status_code == 422
    assert infeasible.json()["code"] == "TIMELINE_DURATION_INFEASIBLE"

    too_long = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": settings.preview_max_seconds * 1_000 + 1,
            "strategy": "equal",
            "expected_input_hash": timeline["input_hash"],
        },
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "TIMELINE_TOO_LONG"

    request = {
        "action": "fit",
        "target_duration_ms": count * 5_000,
        "strategy": "equal",
        "expected_input_hash": timeline["input_hash"],
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                client.put,
                f"/api/v1/projects/{project_id}/timeline/timing",
                json=request,
            )
            for _ in range(2)
        ]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_current_timeline_preview_wins_over_later_updated_stale_preview(runtime):
    client, worker, database, _settings = runtime
    project = _ready_project(client, worker, "timeline-current-preview")
    project_id = project["project"]["id"]
    timeline_a = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    preview_a = client.post(f"/api/v1/projects/{project_id}/preview", json={}).json()["preview"]

    fitted = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": timeline_a["segment_count"] * 4_000,
            "strategy": "equal",
            "expected_input_hash": timeline_a["input_hash"],
        },
    )
    assert fitted.status_code == 200, fitted.text
    timeline_b = fitted.json()
    preview_b = client.post(f"/api/v1/projects/{project_id}/preview", json={}).json()["preview"]
    assert preview_a["input_hash"] != preview_b["input_hash"]

    with database.session() as session:
        stale = session.get(PreviewRender, preview_a["id"])
        stale.updated_at = utcnow() + timedelta(hours=1)

    response = client.get(f"/api/v1/projects/{project_id}/preview")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["timeline"]["input_hash"] == timeline_b["input_hash"]
    assert payload["preview"]["id"] == preview_b["id"]
    assert payload["preview"]["input_hash"] == payload["timeline"]["input_hash"]


def test_asset_metadata_updates_do_not_change_timeline_fingerprint(runtime):
    client, worker, _database, _settings = runtime
    project = _ready_project(client, worker, "timeline-asset-metadata")
    project_id = project["project"]["id"]
    before = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    selected_asset = before["items"][0]["asset"]
    queued_preview = client.post(f"/api/v1/projects/{project_id}/preview", json={}).json()[
        "preview"
    ]

    updated = client.patch(
        f"/api/v1/assets/{selected_asset['id']}",
        json={
            "name": "只更新检索元数据的素材",
            "tags": ["元数据测试", "不改变画面"],
            "keywords": ["名称", "标签", "关键词"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["updated_at"] != selected_asset["updated_at"]

    after = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    assert after["input_hash"] == before["input_hash"]
    preview = client.get(f"/api/v1/projects/{project_id}/preview").json()
    assert preview["preview"]["id"] == queued_preview["id"]
    assert preview["preview"]["input_hash"] == preview["timeline"]["input_hash"]

    fitted = client.put(
        f"/api/v1/projects/{project_id}/timeline/timing",
        json={
            "action": "fit",
            "target_duration_ms": after["segment_count"] * 4_000,
            "strategy": "equal",
            "expected_input_hash": before["input_hash"],
        },
    )
    assert fitted.status_code == 200, fitted.text


def test_segment_content_and_timing_updates_serialize_without_lost_writes(runtime):
    client, worker, _database, _settings = runtime
    project = _ready_project(client, worker, "timeline-mutator-race")
    project_id = project["project"]["id"]
    segment = project["segments"][0]
    timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        content_future = executor.submit(
            client.patch,
            f"/api/v1/segments/{segment['id']}",
            json={"topic": "并发后的主题", "version": segment["version"]},
        )
        timing_future = executor.submit(
            client.put,
            f"/api/v1/projects/{project_id}/timeline/timing",
            json={
                "action": "fit",
                "target_duration_ms": timeline["segment_count"] * 5_000,
                "strategy": "equal",
                "expected_input_hash": timeline["input_hash"],
            },
        )
        responses = [content_future.result(), timing_future.result()]

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert all(response.status_code < 500 for response in responses)


def test_direct_rematch_and_timing_update_share_timeline_mutex(runtime):
    client, worker, _database, _settings = runtime
    project = _ready_project(client, worker, "timeline-rematch-race")
    project_id = project["project"]["id"]
    segment = project["segments"][0]
    timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        rematch_future = executor.submit(
            client.post,
            f"/api/v1/segments/{segment['id']}/rematch",
        )
        timing_future = executor.submit(
            client.put,
            f"/api/v1/projects/{project_id}/timeline/timing",
            json={
                "action": "fit",
                "target_duration_ms": timeline["segment_count"] * 4_500,
                "strategy": "equal",
                "expected_input_hash": timeline["input_hash"],
            },
        )
        responses = [rematch_future.result(), timing_future.result()]

    assert all(response.status_code in {200, 409} for response in responses)
    assert all(response.status_code < 500 for response in responses)


def test_selection_and_reorder_serialize_with_timing_updates(runtime):
    client, worker, _database, _settings = runtime
    project = _ready_project(client, worker, "timeline-other-mutators")
    project_id = project["project"]["id"]
    segment = project["segments"][0]
    replacement = next(
        asset
        for asset in client.get("/api/v1/assets").json()["items"]
        if asset["id"] != segment["selection"]["asset_id"]
    )
    timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        selection_future = executor.submit(
            client.put,
            f"/api/v1/segments/{segment['id']}/selection",
            json={"asset_id": replacement["id"]},
        )
        timing_future = executor.submit(
            client.put,
            f"/api/v1/projects/{project_id}/timeline/timing",
            json={
                "action": "fit",
                "target_duration_ms": timeline["segment_count"] * 4_200,
                "strategy": "equal",
                "expected_input_hash": timeline["input_hash"],
            },
        )
        responses = [selection_future.result(), timing_future.result()]
    assert all(response.status_code in {200, 409} for response in responses)
    assert all(response.status_code < 500 for response in responses)

    current_project = client.get(f"/api/v1/projects/{project_id}").json()
    segment_ids = [item["id"] for item in current_project["segments"]]
    current_timeline = client.get(f"/api/v1/projects/{project_id}/timeline").json()
    with ThreadPoolExecutor(max_workers=2) as executor:
        reorder_future = executor.submit(
            client.put,
            f"/api/v1/projects/{project_id}/segments/order",
            json={"segment_ids": list(reversed(segment_ids))},
        )
        timing_future = executor.submit(
            client.put,
            f"/api/v1/projects/{project_id}/timeline/timing",
            json={
                "action": "fit",
                "target_duration_ms": current_timeline["segment_count"] * 4_400,
                "strategy": "current",
                "expected_input_hash": current_timeline["input_hash"],
            },
        )
        responses = [reorder_future.result(), timing_future.result()]
    assert all(response.status_code in {200, 409} for response in responses)
    assert all(response.status_code < 500 for response in responses)


def test_exact_allocator_handles_caps_and_rounding():
    assert allocate_timeline_durations(8_000, [2_000, 6_000]) == [2_000, 6_000]
    assert allocate_timeline_durations(5_000, [1, 9]) == [1_000, 4_000]
    assert allocate_timeline_durations(50_000, [100, *([1] * 10)]) == [
        30_000,
        *([2_000] * 10),
    ]
    durations = allocate_timeline_durations(61_001, [100, 1, 1])
    assert durations == [30_000, 15_520, 15_480]
    assert sum(durations) == 61_000
    assert all(1_000 <= duration <= 30_000 for duration in durations)
    assert all(duration % 40 == 0 for duration in durations)


def test_existing_sqlite_database_adds_render_duration_column(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "old.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE segments (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                topic VARCHAR(80) NOT NULL,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                start_ms INTEGER,
                end_ms INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        demo_mode=True,
        frontend_dir=tmp_path / "no-frontend",
    )
    database = Database(settings)
    try:
        database.initialize()
        assert "render_duration_ms" in {
            column["name"] for column in inspect(database.engine).get_columns("segments")
        }
    finally:
        database.engine.dispose()
