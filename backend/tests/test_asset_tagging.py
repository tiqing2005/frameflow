from __future__ import annotations

import base64
import threading
from datetime import timedelta

from sqlalchemy import select

from app.models import AIRun, Asset, utcnow
from app.llm import AssetTagSuggestion
from app.services.asset_tagging import (
    PreparedFrame,
    apply_asset_tagging_outcome,
    asset_tagging_snapshot,
    outcome_with_worker_fallback,
)
from app.vision import VisionTagSuggestion
from app.worker import DurableWorker


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def upload_asset(client, name: str, *, tags: str = "", keywords: str = "") -> dict:
    response = client.post(
        "/api/v1/assets",
        data={"name": name, "tags": tags, "keywords": keywords},
        files={"file": (f"{name}.png", PNG, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_queues_and_fill_missing_preserves_user_tags(runtime):
    client, worker, _database, _settings = runtime
    created = upload_asset(client, "用户主题素材", tags="用户标签")

    assert created["tagging_status"] == "queued"
    assert created["tags"] == ["用户标签"]
    assert created["keywords"] == []
    assert worker.run_once() is True

    completed = client.get(f"/api/v1/assets/{created['id']}").json()
    assert completed["tags"] == ["用户标签"]
    assert completed["keywords"]
    assert completed["tagging_status"] == "degraded"
    assert completed["tagging_source"] == "rules"


def test_worker_applies_visual_result_and_records_success(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.vision_provider = "openai-compatible"
    settings.vision_api_key = "test-vision-secret"
    created = upload_asset(client, "视觉成功素材")
    monkeypatch.setattr(
        "app.services.asset_tagging.prepare_asset_frame",
        lambda _snapshot, _settings: PreparedFrame(b"\xff\xd8\xff" + b"x" * 130 + b"\xff\xd9", "image"),
    )
    monkeypatch.setattr(
        "app.services.asset_tagging.suggest_visual_asset_tags",
        lambda _content, _settings: VisionTagSuggestion(
            ["城市", "交通"],
            ["道路", "汽车"],
            "openai-compatible",
            "vision-test-model",
            "succeeded",
            False,
            None,
            None,
            12,
            {"total_tokens": 25},
        ),
    )

    assert worker.run_once() is True
    completed = client.get(f"/api/v1/assets/{created['id']}").json()
    assert completed["tagging_status"] == "succeeded"
    assert completed["tagging_source"] == "vision"
    assert completed["tags"] == ["城市", "交通"]
    with database.session() as session:
        run = session.scalar(
            select(AIRun).where(
                AIRun.operation == "asset_tagging",
                AIRun.provider == "openai-compatible",
            )
        )
        assert run is not None
        assert run.degraded is False
        assert run.status == "succeeded"


def test_visual_failure_falls_back_to_text_llm(runtime, monkeypatch):
    client, worker, _database, settings = runtime
    settings.vision_provider = "openai-compatible"
    settings.vision_api_key = "test-vision-secret"
    settings.llm_provider = "openai-compatible"
    settings.llm_api_key = "test-text-secret"
    created = upload_asset(client, "文本降级素材")
    monkeypatch.setattr(
        "app.services.asset_tagging.prepare_asset_frame",
        lambda _snapshot, _settings: PreparedFrame(b"\xff\xd8\xff" + b"x" * 130 + b"\xff\xd9", "image"),
    )
    monkeypatch.setattr(
        "app.services.asset_tagging.suggest_visual_asset_tags",
        lambda _content, _settings: VisionTagSuggestion(
            [],
            [],
            "openai-compatible",
            "vision-test-model",
            "degraded",
            True,
            "vision_timeout",
            "视觉识别请求超时，已转入文本标签降级流程",
            30,
            {},
        ),
    )
    monkeypatch.setattr(
        "app.services.asset_tagging.suggest_asset_tags_detailed",
        lambda _name, _description, _settings: AssetTagSuggestion(
            ["办公"],
            ["电脑", "效率"],
            "openai-compatible",
            "text-test-model",
            "succeeded",
            False,
            8,
            usage={"total_tokens": 12},
        ),
    )

    assert worker.run_once() is True
    completed = client.get(f"/api/v1/assets/{created['id']}").json()
    assert completed["tagging_status"] == "degraded"
    assert completed["tagging_source"] == "text_llm"
    assert completed["tags"] == ["办公"]
    assert completed["keywords"] == ["电脑", "效率"]


def test_seed_retag_is_idempotent_and_replaces_both_fields(runtime):
    client, worker, database, _settings = runtime
    first = client.post("/api/v1/assets/seed-technology/retag")
    assert first.status_code == 202, first.text
    assert first.json()["tagging_status"] == "queued"
    with database.session() as session:
        generation = session.get(Asset, "seed-technology").tagging_generation

    repeated = client.post("/api/v1/assets/seed-technology/retag")
    assert repeated.status_code == 202
    with database.session() as session:
        assert session.get(Asset, "seed-technology").tagging_generation == generation

    assert worker.run_once() is True
    completed = client.get("/api/v1/assets/seed-technology").json()
    assert completed["tagging_status"] == "degraded"
    assert completed["tagging_source"] == "rules"
    assert completed["tags"]
    assert completed["keywords"]
    with database.session() as session:
        run = session.scalar(
            select(AIRun)
            .where(AIRun.operation == "asset_tagging")
            .order_by(AIRun.created_at.desc())
        )
        assert '"mode":"replace"' in run.output_summary_json


def test_manual_patch_fences_claimed_background_result(runtime):
    client, worker, database, _settings = runtime
    created = upload_asset(client, "人工编辑竞态")
    claim = worker.claim_asset_tagging()
    assert claim is not None and claim.asset_id == created["id"]

    patched = client.patch(
        f"/api/v1/assets/{created['id']}",
        json={"tags": ["人工标签"], "keywords": ["人工关键词"]},
    )
    assert patched.status_code == 200
    worker._process_asset_tagging_task(claim)

    current = client.get(f"/api/v1/assets/{created['id']}").json()
    assert current["tagging_status"] == "idle"
    assert current["tags"] == ["人工标签"]
    assert current["keywords"] == ["人工关键词"]
    with database.session() as session:
        runs = session.scalars(
            select(AIRun).where(AIRun.operation == "asset_tagging")
        ).all()
        assert all(created["id"] not in run.output_summary_json for run in runs)


def test_two_sqlite_workers_claim_distinct_asset_tasks(runtime):
    client, _worker, database, settings = runtime
    first = upload_asset(client, "并发视觉一")
    second = upload_asset(client, "并发视觉二")
    expected = {first["id"], second["id"]}
    workers = [
        DurableWorker(database, settings, worker_id="asset-worker-a"),
        DurableWorker(database, settings, worker_id="asset-worker-b"),
    ]
    barrier = threading.Barrier(2)
    claims = []
    errors: list[BaseException] = []

    def claim(worker: DurableWorker) -> None:
        try:
            barrier.wait(timeout=2)
            claims.append(worker.claim_asset_tagging())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert {claim.asset_id for claim in claims if claim is not None} == expected


def test_two_workers_execute_asset_tagging_concurrently(runtime, monkeypatch):
    client, _worker, database, settings = runtime
    first = upload_asset(client, "并发执行一")
    second = upload_asset(client, "并发执行二")
    expected = {first["id"], second["id"]}
    workers = [
        DurableWorker(database, settings, worker_id="asset-execution-a"),
        DurableWorker(database, settings, worker_id="asset-execution-b"),
    ]
    both_running = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    executions: list[str] = []

    def blocking_tagging(self: DurableWorker, claim) -> None:
        with lock:
            executions.append(claim.asset_id)
            if len(executions) == 2:
                both_running.set()
        assert release.wait(5)

    monkeypatch.setattr(DurableWorker, "_process_asset_tagging_task", blocking_tagging)
    runners = [threading.Thread(target=worker.run_once) for worker in workers]
    for runner in runners:
        runner.start()
    try:
        assert both_running.wait(5)
        assert set(executions) == expected
    finally:
        release.set()
        for runner in runners:
            runner.join(5)
    assert all(not runner.is_alive() for runner in runners)


def test_recovered_attempt_fences_same_stable_worker_id(runtime):
    client, _worker, database, settings = runtime
    created = upload_asset(client, "稳定 Worker 恢复")
    stale_worker = DurableWorker(database, settings, worker_id="stable-asset-worker")
    stale_claim = stale_worker.claim_asset_tagging()
    assert stale_claim is not None
    with database.session() as session:
        stale_snapshot = asset_tagging_snapshot(session, stale_claim)
        assert stale_snapshot is not None
        asset = session.get(Asset, created["id"])
        asset.tagging_lease_expires_at = utcnow() - timedelta(seconds=1)

    recovered_worker = DurableWorker(database, settings, worker_id="stable-asset-worker")
    recovered_claim = recovered_worker.claim_asset_tagging()
    assert recovered_claim is not None
    assert recovered_claim.generation == stale_claim.generation
    assert recovered_claim.attempt == stale_claim.attempt + 1
    assert stale_worker._renew_asset_tagging_lease(stale_claim) is False

    stale_outcome = outcome_with_worker_fallback(
        stale_snapshot,
        "stale_attempt",
        "旧执行结果不应写入",
    )
    with database.session() as session:
        assert apply_asset_tagging_outcome(session, stale_claim, stale_outcome) is False
    recovered_worker.process_asset_tagging(recovered_claim)
    assert client.get(f"/api/v1/assets/{created['id']}").json()["tagging_status"] == "degraded"


def test_retag_upgrades_active_fill_missing_to_replace(runtime):
    client, worker, database, _settings = runtime
    created = upload_asset(client, "覆盖旧标签", tags="用户标签")
    old_claim = worker.claim_asset_tagging()
    assert old_claim is not None

    retagged = client.post(f"/api/v1/assets/{created['id']}/retag")
    assert retagged.status_code == 202
    with database.session() as session:
        asset = session.get(Asset, created["id"])
        assert asset.tagging_mode == "replace"
        assert asset.tagging_generation == old_claim.generation + 1
        generation = asset.tagging_generation
    repeated = client.post(f"/api/v1/assets/{created['id']}/retag")
    assert repeated.status_code == 202
    with database.session() as session:
        assert session.get(Asset, created["id"]).tagging_generation == generation

    worker._process_asset_tagging_task(old_claim)
    new_claim = worker.claim_asset_tagging()
    assert new_claim is not None and new_claim.generation == generation
    worker.process_asset_tagging(new_claim)
    completed = client.get(f"/api/v1/assets/{created['id']}").json()
    assert completed["tagging_status"] == "degraded"
    assert completed["tags"] != ["用户标签"]


def test_crash_attempt_exhaustion_finishes_with_rules(runtime):
    client, worker, database, _settings = runtime
    created = upload_asset(client, "崩溃恢复耗尽")
    with database.session() as session:
        asset = session.get(Asset, created["id"])
        asset.tagging_status = "running"
        asset.tagging_attempt = 3
        asset.tagging_lease_owner = "crashed-worker"
        asset.tagging_lease_expires_at = utcnow() - timedelta(seconds=1)

    assert worker.claim_asset_tagging() is None
    completed = client.get(f"/api/v1/assets/{created['id']}").json()
    assert completed["tagging_status"] == "degraded"
    assert completed["tagging_source"] == "rules"
    assert completed["tags"]
    assert completed["keywords"]


def test_health_counts_running_asset_tagging_as_busy(runtime):
    client, worker, _database, _settings = runtime
    created = upload_asset(client, "健康状态视觉任务")
    worker.heartbeat()
    claim = worker.claim_asset_tagging()
    assert claim is not None

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    worker_check = ready.json()["checks"]["worker"]
    assert created["id"] in worker_check["active_asset_tagging_ids"]
    assert worker_check["capacity"]["busy"] == 1
