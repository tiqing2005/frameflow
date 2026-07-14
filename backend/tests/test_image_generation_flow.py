from __future__ import annotations

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from PIL import Image
import pytest
from sqlalchemy import func, select

from app.image_generation import GeneratedImage, ImageGenerationFailure
from app.image_worker import DurableImageWorker
from app.models import AIRun, Asset, ImageGeneration, Selection, utcnow


def generated_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1280, 720), (24, 80, 144)).save(output, format="PNG")
    return output.getvalue()


def generated_result() -> GeneratedImage:
    return GeneratedImage(
        png_bytes=generated_png(),
        width=1280,
        height=720,
        provider="openai-compatible",
        model="unit-image-model",
        duration_ms=12,
        usage={"input_tokens": 3, "total_tokens": 3},
    )


def enable_image_generation(settings) -> None:
    settings.image_api_base_url = "https://images.example.invalid/v1"
    settings.image_api_key = "unit-test-image-key"
    settings.image_model = "unit-image-model"
    settings.image_daily_limit = 0
    settings.image_max_pending = 20
    settings.image_draft_retention_hours = 24


def create_generation(client, *, key: str, prompt: str = "城市夜景，无文字", **extra):
    payload = {
        "prompt": prompt,
        "name": "AI 城市夜景",
        "aspect_ratio": "16:9",
        **extra,
    }
    response = client.post(
        "/api/v1/image-generations",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response


def complete_generation(client, database, settings, monkeypatch, *, key: str):
    created = create_generation(client, key=key).json()["generation"]
    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    image_worker = DurableImageWorker(
        database, settings, worker_id=f"pytest-image-{key}"
    )
    assert image_worker.run_once() is True
    detail = client.get(f"/api/v1/image-generations/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["generation"]["status"] == "succeeded"
    return created, image_worker


def create_ready_segment(client, core_worker, *, key: str = "image-segment-project"):
    created = client.post(
        "/api/v1/projects/text",
        json={
            "title": "片段生图测试",
            "text": "智能城市通过绿色交通和公共空间，让生活更高效、更舒适。",
        },
        headers={"Idempotency-Key": key},
    )
    assert created.status_code == 202, created.text
    assert core_worker.run_once() is True
    project_id = created.json()["project"]["id"]
    detail = client.get(f"/api/v1/projects/{project_id}")
    assert detail.status_code == 200
    return detail.json()["segments"][0]


def test_create_is_idempotent_and_same_key_different_request_conflicts(runtime):
    client, _worker, database, settings = runtime
    enable_image_generation(settings)

    first = create_generation(client, key="image-create-idempotency")
    replay = create_generation(client, key="image-create-idempotency")
    conflict = client.post(
        "/api/v1/image-generations",
        json={"prompt": "完全不同的提示词", "aspect_ratio": "16:9"},
        headers={"Idempotency-Key": "image-create-idempotency"},
    )

    assert first.headers["Idempotent-Replay"] == "false"
    assert replay.headers["Idempotent-Replay"] == "true"
    assert first.json()["generation"]["id"] == replay.json()["generation"]["id"]
    assert replay.json()["idempotent_replay"] is True
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    listed = client.get("/api/v1/image-generations")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ImageGeneration)) == 1


def test_create_requires_dedicated_configuration_and_enforces_queue_limit(runtime):
    client, _worker, _database, settings = runtime
    not_configured = client.post(
        "/api/v1/image-generations", json={"prompt": "不会生成"}
    )
    assert not_configured.status_code == 503
    assert not_configured.json()["code"] == "IMAGE_GENERATION_NOT_CONFIGURED"

    enable_image_generation(settings)
    settings.image_max_pending = 1
    create_generation(client, key="image-queue-first")
    full = client.post(
        "/api/v1/image-generations",
        json={"prompt": "第二个排队任务"},
        headers={"Idempotency-Key": "image-queue-second"},
    )
    assert full.status_code == 429
    assert full.json()["code"] == "IMAGE_QUEUE_FULL"
    assert full.json()["retryable"] is True


def test_success_content_accept_is_idempotent_and_tagging_degrades_honestly(
    runtime, monkeypatch
):
    client, core_worker, database, settings = runtime
    enable_image_generation(settings)
    created, _image_worker = complete_generation(
        client, database, settings, monkeypatch, key="image-accept-flow"
    )

    content = client.get(f"/api/v1/image-generations/{created['id']}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/png")
    assert content.headers["cache-control"] == "private, no-store"
    assert content.content.startswith(b"\x89PNG\r\n\x1a\n")

    accepted = client.post(
        f"/api/v1/image-generations/{created['id']}/accept", json={}
    )
    replay = client.post(
        f"/api/v1/image-generations/{created['id']}/accept", json={}
    )
    assert accepted.status_code == 200, accepted.text
    assert replay.status_code == 200, replay.text
    assert accepted.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    asset_id = accepted.json()["asset"]["id"]
    assert replay.json()["asset"]["id"] == asset_id
    assert accepted.json()["asset"]["tagging_status"] == "queued"

    # VISION/LLM are deliberately unconfigured in this fixture. The existing
    # tagging queue must complete with an explicit rules degradation, not claim
    # that Gemini inspected the image.
    assert core_worker.run_once() is True
    tagged = client.get(f"/api/v1/assets/{asset_id}")
    assert tagged.status_code == 200
    assert tagged.json()["tagging_status"] == "degraded"
    assert tagged.json()["tagging_source"] == "rules"
    assert tagged.json()["tags"]
    assert tagged.json()["keywords"]

    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        asset = session.get(Asset, asset_id)
        assert generation.asset_id == asset_id
        assert generation.output_storage_path is None
        assert asset is not None and Path(asset.storage_path).is_file()
        assert (
            session.scalar(
                select(func.count()).select_from(Asset).where(Asset.id == asset_id)
            )
            == 1
        )
        operations = session.scalars(
            select(AIRun.operation).where(
                AIRun.operation.in_(("image_generation", "asset_tagging"))
            )
        ).all()
        assert "image_generation" in operations
        assert "asset_tagging" in operations


def test_retry_only_requeues_retryable_failure(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-retry-flow").json()["generation"]
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.max_attempts = 1

    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ImageGenerationFailure(
                "IMAGE_PROVIDER_BUSY", "服务繁忙", retryable=True
            )
        ),
    )
    image_worker = DurableImageWorker(database, settings, worker_id="pytest-image-retry")
    assert image_worker.run_once() is True
    failed = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert failed["status"] == "failed"
    assert failed["retryable"] is True

    retried = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["generation"]["status"] == "queued"
    assert retried.json()["generation"]["max_attempts"] == 2

    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    assert image_worker.run_once() is True
    assert (
        client.get(f"/api/v1/image-generations/{created['id']}")
        .json()["generation"]["status"]
        == "succeeded"
    )
    invalid = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "IMAGE_RETRY_INVALID_STATE"


def test_manual_retry_has_a_hard_four_attempt_limit(runtime):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-manual-retry-limit").json()[
        "generation"
    ]
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.status = "failed"
        generation.retryable = True
        generation.attempt = 3
        generation.max_attempts = 3

    last_allowed = client.post(
        f"/api/v1/image-generations/{created['id']}/retry"
    )
    assert last_allowed.status_code == 202, last_allowed.text
    assert last_allowed.json()["generation"]["status"] == "queued"
    assert last_allowed.json()["generation"]["max_attempts"] == 4

    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.status = "failed"
        generation.retryable = True
        generation.attempt = 4
        generation.max_attempts = 4

    exhausted = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert exhausted.status_code == 409
    assert exhausted.json()["code"] == "IMAGE_RETRY_LIMIT_REACHED"
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        assert generation.status == "failed"
        assert generation.attempt == 4
        assert generation.max_attempts == 4


@pytest.mark.parametrize(
    ("failure_code", "retryable", "ambiguous", "expected_code"),
    [
        ("IMAGE_PROVIDER_BUSY", True, False, "IMAGE_PROVIDER_BUSY"),
        (
            "IMAGE_NETWORK_ERROR",
            False,
            True,
            "IMAGE_PROVIDER_RESULT_UNKNOWN",
        ),
    ],
)
def test_failure_at_hard_limit_is_publicly_non_retryable(
    runtime,
    monkeypatch,
    failure_code,
    retryable,
    ambiguous,
    expected_code,
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key=f"image-hard-limit-{failure_code}").json()[
        "generation"
    ]
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.attempt = 3
        generation.max_attempts = 4

    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ImageGenerationFailure(
                failure_code,
                "上游调用失败",
                retryable=retryable,
                ambiguous_submission=ambiguous,
            )
        ),
    )
    worker = DurableImageWorker(database, settings, worker_id="image-hard-limit-worker")
    assert worker.run_once() is True
    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert detail["status"] == "failed"
    assert detail["attempt"] == 4
    assert detail["retryable"] is False
    assert detail["error_code"] == expected_code
    assert "4 次调用上限" in detail["error_message"]

    retried = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert retried.status_code == 409
    assert retried.json()["code"] == "IMAGE_RETRY_LIMIT_REACHED"


def test_expired_lease_recovers_and_old_execution_cannot_publish(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-lease-recovery").json()["generation"]
    old_worker = DurableImageWorker(database, settings, worker_id="old-image-worker")
    old_claim = old_worker.claim()
    assert old_claim is not None
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.lease_expires_at = utcnow() - timedelta(seconds=1)

    recovered_worker = DurableImageWorker(
        database, settings, worker_id="recovered-image-worker"
    )
    recovered_claim = recovered_worker.claim()
    assert recovered_claim is not None
    assert recovered_claim.execution_generation > old_claim.execution_generation
    assert recovered_claim.attempt == old_claim.attempt
    assert recovered_claim.recovered is True

    applied, _auto_import = old_worker._apply_success(old_claim, generated_result())
    assert applied is False
    draft_root = settings.data_dir / "private" / "image-generations"
    assert list(draft_root.glob("*.png")) == []

    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    recovered_worker.process(recovered_claim)
    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()
    assert detail["generation"]["status"] == "succeeded"
    assert detail["generation"]["attempt"] == 1


def test_submitted_marker_exists_before_provider_and_success_cleans_staging(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-submission-barrier").json()[
        "generation"
    ]
    image_worker = DurableImageWorker(
        database, settings, worker_id="image-submission-barrier-worker"
    )

    def assert_barrier_then_generate(_prompt, _ratio, _settings):
        directory = image_worker._staging_directory(created["id"])
        assert len(list(directory.glob("*.submitted.json"))) == 1
        assert list(directory.glob("*.ready.json")) == []
        return generated_result()

    monkeypatch.setattr("app.image_worker.generate_image", assert_barrier_then_generate)
    assert image_worker.run_once() is True
    assert not image_worker._staging_directory(created["id"]).exists()


def test_ready_bundle_recovers_at_attempt_limit_without_second_provider_call(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-ready-crash-recovery").json()[
        "generation"
    ]
    with database.session() as session:
        session.get(ImageGeneration, created["id"]).max_attempts = 1

    provider_calls = 0

    def fake_generate(_prompt, _ratio, _settings):
        nonlocal provider_calls
        provider_calls += 1
        return generated_result()

    first_worker = DurableImageWorker(
        database, settings, worker_id="image-ready-before-db-crash"
    )
    monkeypatch.setattr("app.image_worker.generate_image", fake_generate)
    monkeypatch.setattr(
        first_worker,
        "_apply_success",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("forced crash after ready bundle")
        ),
    )
    assert first_worker.run_once() is True
    assert provider_calls == 1
    assert len(
        list(first_worker._staging_directory(created["id"]).glob("*.ready.json"))
    ) == 1
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        assert generation.status == "running"
        generation.lease_expires_at = utcnow() - timedelta(seconds=1)

    recovered_worker = DurableImageWorker(
        database, settings, worker_id="image-ready-recovery-worker"
    )
    recovered_claim = recovered_worker.claim()
    assert recovered_claim is not None
    assert recovered_claim.reuse_staged is True
    assert recovered_claim.attempt == 1
    recovered_worker.process(recovered_claim)

    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert detail["status"] == "succeeded"
    assert detail["attempt"] == 1
    assert provider_calls == 1
    assert not recovered_worker._staging_directory(created["id"]).exists()


def test_ready_persistence_failure_is_result_unknown_until_manual_retry(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-ready-persist-failure").json()[
        "generation"
    ]
    provider_calls = 0

    def fake_generate(_prompt, _ratio, _settings):
        nonlocal provider_calls
        provider_calls += 1
        return generated_result()

    worker = DurableImageWorker(
        database, settings, worker_id="image-ready-persist-failure-worker"
    )

    def fail_before_ready(claim, _snapshot, result):
        _submitted, result_path, _ready = worker._bundle_paths(claim)
        worker._atomic_write(result_path, result.png_bytes)
        raise OSError("forced ready marker persistence failure")

    monkeypatch.setattr("app.image_worker.generate_image", fake_generate)
    monkeypatch.setattr(worker, "_persist_ready_bundle", fail_before_ready)
    assert worker.run_once() is True
    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert detail["status"] == "failed"
    assert detail["error_code"] == "IMAGE_PROVIDER_RESULT_UNKNOWN"
    assert detail["retryable"] is True
    assert provider_calls == 1
    directory = worker._staging_directory(created["id"])
    assert len(list(directory.glob("*.submitted.json"))) == 1
    assert len(list(directory.glob("*.result.png"))) == 1
    assert list(directory.glob("*.ready.json")) == []

    # A normal worker pass cannot silently cross the paid boundary again.
    assert DurableImageWorker(
        database, settings, worker_id="image-no-auto-retry-worker"
    ).run_once() is False
    assert provider_calls == 1
    retried = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert retried.status_code == 202, retried.text


def test_submitted_without_ready_requires_explicit_manual_retry(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-submitted-result-unknown").json()[
        "generation"
    ]
    first_worker = DurableImageWorker(
        database, settings, worker_id="image-submitted-crashed-worker"
    )
    first_claim = first_worker.claim()
    assert first_claim is not None
    snapshot = first_worker._snapshot(first_claim)
    assert snapshot is not None
    first_worker._write_submission_marker(first_claim, snapshot)
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.lease_expires_at = utcnow() - timedelta(seconds=1)

    recovered_worker = DurableImageWorker(
        database, settings, worker_id="image-ambiguous-recovery-worker"
    )
    assert recovered_worker.claim() is None
    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert detail["status"] == "failed"
    assert detail["error_code"] == "IMAGE_PROVIDER_RESULT_UNKNOWN"
    assert detail["retryable"] is True
    assert detail["attempt"] == 1

    retried = client.post(f"/api/v1/image-generations/{created['id']}/retry")
    assert retried.status_code == 202, retried.text
    manual_claim = recovered_worker.claim()
    assert manual_claim is not None
    assert manual_claim.manual_retry_authorized is True
    assert manual_claim.attempt == 2
    provider_calls = 0

    def fake_generate(_prompt, _ratio, _settings):
        nonlocal provider_calls
        provider_calls += 1
        return generated_result()

    monkeypatch.setattr("app.image_worker.generate_image", fake_generate)
    recovered_worker.process(manual_claim)
    assert provider_calls == 1
    assert (
        client.get(f"/api/v1/image-generations/{created['id']}")
        .json()["generation"]["status"]
        == "succeeded"
    )


def test_expiry_and_orphan_cleanup_remove_staging_sidecars(runtime):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-staging-retention").json()[
        "generation"
    ]
    worker = DurableImageWorker(database, settings, worker_id="image-cleanup-worker")
    directory = worker._staging_directory(created["id"])
    directory.mkdir(parents=True)
    (directory / "a000001-g000001.submitted.json").write_text("{}", encoding="utf-8")
    (directory / "a000001-g000001.result.png").write_bytes(generated_png())
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.status = "failed"
        generation.retryable = True
        generation.expires_at = utcnow() - timedelta(seconds=1)

    worker.cleanup_expired_drafts()
    assert not directory.exists()

    orphan = worker.staging_root / "missing-generation"
    orphan.mkdir(parents=True)
    partial = orphan / "a000001-g000001.result.png"
    partial.write_bytes(generated_png())
    old_timestamp = utcnow().timestamp() - 600
    os.utime(partial, (old_timestamp, old_timestamp))
    os.utime(orphan, (old_timestamp, old_timestamp))
    worker.cleanup_staging_orphans()
    assert not orphan.exists()


def test_output_orphan_cleanup_preserves_referenced_and_unowned_files(runtime):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-output-orphan-cleanup").json()[
        "generation"
    ]
    worker = DurableImageWorker(database, settings, worker_id="image-output-cleanup-worker")
    root = settings.data_dir / "private" / "image-generations"
    root.mkdir(parents=True, exist_ok=True)
    referenced = root / f"{created['id']}-1-deadbeef.png"
    orphan = root / f"{created['id']}-999-cafebabe.png"
    temporary = root / f".{created['id']}-{'a' * 32}.tmp"
    unowned = root / "manual-reference.png"
    for path in (referenced, orphan, temporary, unowned):
        path.write_bytes(generated_png())
    old_timestamp = utcnow().timestamp() - 600
    for path in (referenced, orphan, temporary, unowned):
        os.utime(path, (old_timestamp, old_timestamp))
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        generation.status = "succeeded"
        generation.output_storage_path = str(referenced)

    worker.cleanup_output_orphans()
    assert referenced.is_file()
    assert not orphan.exists()
    assert not temporary.exists()
    assert unowned.is_file()


def test_cancel_fences_provider_response_that_arrives_late(runtime):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-cancel-late").json()["generation"]
    image_worker = DurableImageWorker(database, settings, worker_id="late-image-worker")
    claim = image_worker.claim()
    assert claim is not None

    canceled = client.post(f"/api/v1/image-generations/{created['id']}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["generation"]["status"] == "canceled"
    applied, _auto_import = image_worker._apply_success(claim, generated_result())
    assert applied is False

    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()
    assert detail["generation"]["status"] == "canceled"
    assert detail["generation"]["content_url"] is None
    assert list(
        (settings.data_dir / "private" / "image-generations").glob("*.png")
    ) == []


def test_cancel_during_provider_call_removes_late_ready_bundle(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(client, key="image-cancel-late-staging").json()[
        "generation"
    ]

    def cancel_then_return(_prompt, _ratio, _settings):
        canceled = client.post(
            f"/api/v1/image-generations/{created['id']}/cancel"
        )
        assert canceled.status_code == 200
        return generated_result()

    monkeypatch.setattr("app.image_worker.generate_image", cancel_then_return)
    worker = DurableImageWorker(
        database, settings, worker_id="image-cancel-late-staging-worker"
    )
    assert worker.run_once() is True
    detail = client.get(f"/api/v1/image-generations/{created['id']}").json()[
        "generation"
    ]
    assert detail["status"] == "canceled"
    assert detail["content_url"] is None
    assert not worker._staging_directory(created["id"]).exists()


def test_stop_keeps_inflight_lease_renewal_until_process_finishes(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    create_generation(client, key="image-graceful-stop-lease")
    worker = DurableImageWorker(
        database, settings, worker_id="image-graceful-stop-worker"
    )
    claim = worker.claim()
    assert claim is not None
    stop_event = threading.Event()
    lease_lost = threading.Event()
    renewals = 0

    def renew_once(_claim):
        nonlocal renewals
        renewals += 1
        stop_event.set()
        return True

    monkeypatch.setattr(worker, "_renew_lease", renew_once)
    monkeypatch.setattr(
        DurableImageWorker,
        "lease_renew_interval",
        property(lambda _worker: 0.001),
    )
    worker.stop()
    worker._lease_loop(claim, stop_event, lease_lost)
    assert renewals == 1
    assert lease_lost.is_set() is False


def test_segment_version_conflict_preserves_draft_then_current_version_can_select(
    runtime, monkeypatch
):
    client, core_worker, database, settings = runtime
    enable_image_generation(settings)
    segment = create_ready_segment(client, core_worker)
    created = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json={"aspect_ratio": "16:9"},
        headers={"Idempotency-Key": "segment-image-version"},
    )
    assert created.status_code == 202, created.text
    generation = created.json()["generation"]
    assert generation["segment_version"] == segment["version"]
    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    assert DurableImageWorker(database, settings, worker_id="segment-image-worker").run_once()

    edited = client.patch(
        f"/api/v1/segments/{segment['id']}",
        json={"text": "字幕内容已经更新，旧图片不能静默覆盖。", "version": segment["version"]},
    )
    assert edited.status_code == 200, edited.text
    current_version = edited.json()["version"]
    conflict = client.post(
        f"/api/v1/image-generations/{generation['id']}/accept",
        json={
            "select_for_segment": True,
            "expected_segment_version": segment["version"],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IMAGE_SEGMENT_VERSION_CONFLICT"
    assert (
        client.get(f"/api/v1/image-generations/{generation['id']}")
        .json()["generation"]["asset_id"]
        is None
    )

    accepted = client.post(
        f"/api/v1/image-generations/{generation['id']}/accept",
        json={
            "select_for_segment": True,
            "expected_segment_version": current_version,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["selection"]["source"] == "generated"
    assert accepted.json()["selection"]["asset_id"] == accepted.json()["asset"]["id"]
    with database.session() as session:
        selection = session.scalar(
            select(Selection).where(Selection.segment_id == segment["id"])
        )
        assert selection.asset_id == accepted.json()["asset"]["id"]


def test_segment_idempotency_replay_keeps_original_task_after_segment_update(runtime):
    client, core_worker, _database, settings = runtime
    enable_image_generation(settings)
    segment = create_ready_segment(
        client, core_worker, key="image-segment-idempotency-project"
    )
    headers = {"Idempotency-Key": "segment-image-stable-replay"}
    payload = {"aspect_ratio": "16:9", "prompt": "绿色城市交通配图"}
    first = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 202, first.text
    original = first.json()["generation"]

    edited = client.patch(
        f"/api/v1/segments/{segment['id']}",
        json={
            "text": "字幕已经更新，但相同幂等请求仍应指向首次创建的付费任务。",
            "version": segment["version"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] > segment["version"]

    replay = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 202, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["generation"]["id"] == original["id"]
    assert replay.json()["generation"]["segment_version"] == original["segment_version"]


def test_already_imported_generation_can_select_later_with_version_guard(
    runtime, monkeypatch
):
    client, core_worker, database, settings = runtime
    enable_image_generation(settings)
    segment = create_ready_segment(
        client, core_worker, key="image-late-select-project"
    )
    created = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json={
            "prompt": "绿色城市骑行场景",
            "aspect_ratio": "16:9",
            "auto_import": True,
            "auto_select": False,
        },
        headers={"Idempotency-Key": "image-late-select"},
    )
    assert created.status_code == 202, created.text
    generation_id = created.json()["generation"]["id"]
    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    assert DurableImageWorker(
        database, settings, worker_id="image-auto-import-worker"
    ).run_once()
    imported = client.get(f"/api/v1/image-generations/{generation_id}").json()
    asset_id = imported["generation"]["asset_id"]
    assert imported["generation"]["status"] == "succeeded"
    assert asset_id is not None

    edited = client.patch(
        f"/api/v1/segments/{segment['id']}",
        json={
            "text": "编辑后的绿色交通字幕，需要用户确认再应用已有生成图。",
            "version": segment["version"],
        },
    )
    assert edited.status_code == 200, edited.text
    current_version = edited.json()["version"]

    stale = client.post(
        f"/api/v1/image-generations/{generation_id}/accept",
        json={
            "select_for_segment": True,
            "expected_segment_version": segment["version"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "IMAGE_SEGMENT_VERSION_CONFLICT"
    assert (
        client.get(f"/api/v1/image-generations/{generation_id}")
        .json()["generation"]["asset_id"]
        == asset_id
    )

    selected = client.post(
        f"/api/v1/image-generations/{generation_id}/accept",
        json={
            "select_for_segment": True,
            "expected_segment_version": current_version,
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["idempotent_replay"] is True
    assert selected.json()["asset"]["id"] == asset_id
    assert selected.json()["selection"]["asset_id"] == asset_id
    assert selected.json()["selection"]["source"] == "generated"


def test_inactive_imported_generation_cannot_be_selected_later(runtime, monkeypatch):
    client, core_worker, database, settings = runtime
    enable_image_generation(settings)
    segment = create_ready_segment(
        client, core_worker, key="image-inactive-late-select-project"
    )
    created = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json={
            "prompt": "停用素材不能重新选择",
            "aspect_ratio": "16:9",
            "auto_import": True,
            "auto_select": False,
        },
        headers={"Idempotency-Key": "image-inactive-late-select"},
    )
    assert created.status_code == 202, created.text
    generation_id = created.json()["generation"]["id"]
    monkeypatch.setattr(
        "app.image_worker.generate_image",
        lambda _prompt, _ratio, _settings: generated_result(),
    )
    assert DurableImageWorker(
        database, settings, worker_id="image-inactive-import-worker"
    ).run_once()
    imported = client.get(f"/api/v1/image-generations/{generation_id}").json()
    asset_id = imported["generation"]["asset_id"]
    assert asset_id is not None

    deactivated = client.patch(f"/api/v1/assets/{asset_id}", json={"active": False})
    assert deactivated.status_code == 200, deactivated.text
    with database.session() as session:
        before = session.scalar(
            select(Selection).where(Selection.segment_id == segment["id"])
        )
        before_state = (before.asset_id, before.source) if before else None

    selected = client.post(
        f"/api/v1/image-generations/{generation_id}/accept",
        json={
            "select_for_segment": True,
            "expected_segment_version": segment["version"],
        },
    )
    assert selected.status_code == 409
    assert selected.json()["code"] == "ASSET_INACTIVE"
    with database.session() as session:
        after = session.scalar(
            select(Selection).where(Selection.segment_id == segment["id"])
        )
        after_state = (after.asset_id, after.source) if after else None
    assert after_state == before_state


def test_concurrent_accept_is_idempotent(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created, _image_worker = complete_generation(
        client, database, settings, monkeypatch, key="image-concurrent-accept"
    )
    before_assets = client.get("/api/v1/assets").json()["total"]

    def accept_once(_index):
        return client.post(
            f"/api/v1/image-generations/{created['id']}/accept", json={}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(accept_once, range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    asset_ids = {response.json()["asset"]["id"] for response in responses}
    assert len(asset_ids) == 1
    assert sorted(response.json()["idempotent_replay"] for response in responses) == [
        False,
        True,
    ]
    assert client.get("/api/v1/assets").json()["total"] == before_assets + 1


def test_accept_and_discard_race_has_one_valid_outcome(runtime, monkeypatch):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created, _image_worker = complete_generation(
        client, database, settings, monkeypatch, key="image-accept-discard-race"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        accept_future = pool.submit(
            client.post,
            f"/api/v1/image-generations/{created['id']}/accept",
            json={},
        )
        discard_future = pool.submit(
            client.delete, f"/api/v1/image-generations/{created['id']}"
        )
        accepted = accept_future.result()
        discarded = discard_future.result()

    assert (accepted.status_code, discarded.status_code) in {(200, 409), (409, 204)}
    detail = client.get(
        f"/api/v1/image-generations/{created['id']}?include_discarded=true"
    ).json()["generation"]
    if accepted.status_code == 200:
        assert accepted.json()["asset"]["id"] == detail["asset_id"]
        assert detail["discarded_at"] is None
        assert discarded.json()["code"] == "IMAGE_ALREADY_ACCEPTED"
    else:
        assert accepted.json()["code"] == "IMAGE_DRAFT_DISCARDED"
        assert detail["asset_id"] is None
        assert detail["discarded_at"] is not None


def test_accept_transaction_failure_keeps_draft_and_removes_partial_asset(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created, _image_worker = complete_generation(
        client, database, settings, monkeypatch, key="image-accept-rollback"
    )
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        draft_path = Path(generation.output_storage_path)
        asset_count = session.scalar(select(func.count()).select_from(Asset))
    assert draft_path.is_file()

    from app.services import image_generations as generation_service

    real_add_audit = generation_service.add_audit

    def fail_after_asset_copy(session, project_id, entity_type, entity_id, action, **kwargs):
        if action == "image_generation.accepted":
            raise RuntimeError("forced transaction rollback")
        return real_add_audit(
            session, project_id, entity_type, entity_id, action, **kwargs
        )

    monkeypatch.setattr(generation_service, "add_audit", fail_after_asset_copy)
    # TestClient intentionally re-raises server exceptions; production error
    # middleware still converts this path to the unified 500 response.
    with pytest.raises(RuntimeError, match="forced transaction rollback"):
        client.post(f"/api/v1/image-generations/{created['id']}/accept", json={})

    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        assert generation.asset_id is None
        assert generation.output_storage_path == str(draft_path)
        assert draft_path.is_file()
        assert session.scalar(select(func.count()).select_from(Asset)) == asset_count
    generated_assets = settings.data_dir / "media" / "uploads" / "assets"
    assert not any(path.name != draft_path.name for path in generated_assets.glob("*.png"))


def test_auto_import_recovers_process_window_without_second_provider_call(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created = create_generation(
        client, key="image-auto-import-recovery", auto_import=True
    ).json()["generation"]
    provider_calls = 0

    def fake_generate(_prompt, _ratio, _settings):
        nonlocal provider_calls
        provider_calls += 1
        return generated_result()

    monkeypatch.setattr("app.image_worker.generate_image", fake_generate)
    first_worker = DurableImageWorker(
        database, settings, worker_id="image-before-auto-import-crash"
    )
    monkeypatch.setattr(first_worker, "_auto_accept", lambda _generation_id: None)
    assert first_worker.run_once() is True
    pending_import = client.get(
        f"/api/v1/image-generations/{created['id']}"
    ).json()["generation"]
    assert pending_import["status"] == "succeeded"
    assert pending_import["asset_id"] is None
    assert provider_calls == 1

    recovered_worker = DurableImageWorker(
        database, settings, worker_id="image-auto-import-recovery-worker"
    )
    assert recovered_worker.run_once() is True
    recovered = client.get(f"/api/v1/image-generations/{created['id']}").json()
    assert recovered["generation"]["status"] == "succeeded"
    assert recovered["generation"]["asset_id"] is not None
    assert recovered["asset"]["id"] == recovered["generation"]["asset_id"]
    assert recovered["asset"]["tagging_status"] == "queued"
    assert provider_calls == 1


def test_after_commit_draft_cleanup_failure_does_not_turn_accept_into_500(
    runtime, monkeypatch
):
    client, _core_worker, database, settings = runtime
    enable_image_generation(settings)
    created, _image_worker = complete_generation(
        client, database, settings, monkeypatch, key="image-cleanup-failure"
    )
    with database.session() as session:
        draft_path = Path(
            session.get(ImageGeneration, created["id"]).output_storage_path
        ).resolve()
    real_unlink = Path.unlink

    def fail_only_draft_cleanup(path: Path, *args, **kwargs):
        if path.resolve(strict=False) == draft_path:
            raise OSError("forced after_commit cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_only_draft_cleanup)
    accepted = client.post(
        f"/api/v1/image-generations/{created['id']}/accept", json={}
    )
    assert accepted.status_code == 200, accepted.text
    asset_id = accepted.json()["asset"]["id"]
    with database.session() as session:
        generation = session.get(ImageGeneration, created["id"])
        assert generation.asset_id == asset_id
        assert session.get(Asset, asset_id) is not None


def test_postgresql_write_lock_uses_transaction_advisory_lock():
    from app.services.image_generations import _lock_writes

    statements: list[str] = []

    class FakeSession:
        def get_bind(self):
            return type(
                "Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()}
            )()

        def execute(self, statement, *args, **kwargs):
            statements.append(str(statement))

    assert _lock_writes(FakeSession()) == "postgresql"
    assert any("pg_advisory_xact_lock" in statement for statement in statements)
