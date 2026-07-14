from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.models import Job, JobEvent, Project, Source, utcnow
from app.worker import DurableWorker, WorkerFailure


SAMPLE_TEXT = (
    "人工智能正在改变我们的工作方式，让团队可以更快分析复杂数据。"
    "与此同时，数据安全和用户隐私必须成为产品设计的一部分。"
    "高效协作并不只是开会，而是让每个人围绕共同目标持续行动。"
    "完成工作以后，我们也应该走进绿色自然，通过运动保持健康活力。"
)


def create_project(client, title="全流程验收", text=SAMPLE_TEXT, key="flow-key"):
    response = client.post(
        "/api/v1/projects/text",
        json={"title": title, "text": text},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_concurrent_idempotent_project_create_returns_one_resource(runtime):
    client, _worker, database, _settings = runtime

    def create_once():
        return client.post(
            "/api/v1/projects/text",
            json={"title": "并发幂等", "text": SAMPLE_TEXT},
            headers={"Idempotency-Key": "concurrent-idempotency-key"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: create_once(), range(2)))

    assert [response.status_code for response in responses] == [202, 202]
    payloads = [response.json() for response in responses]
    assert payloads[0]["project"]["id"] == payloads[1]["project"]["id"]
    assert payloads[0]["job"]["id"] == payloads[1]["job"]["id"]
    assert sorted(payload["idempotent_replay"] for payload in payloads) == [False, True]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Project)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_create_job_result_edit_select_reorder_and_refresh(runtime):
    client, worker, _database, _settings = runtime

    first = create_project(client)
    replay = create_project(client)
    assert replay["project"]["id"] == first["project"]["id"]
    assert replay["job"]["id"] == first["job"]["id"]
    assert replay["idempotent_replay"] is True
    assert client.get("/api/v1/projects").json()["total"] == 1

    conflict = client.post(
        "/api/v1/projects/text",
        json={"title": "不同请求", "text": "相同幂等键不能复用到不同内容。"},
        headers={"Idempotency-Key": "flow-key"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.get("/api/v1/projects").json()["total"] == 1

    queued = client.get(f"/api/v1/jobs/{first['job']['id']}")
    assert queued.status_code == 200
    assert queued.json()["job"]["status"] == "queued"
    assert worker.run_once() is True

    job_response = client.get(f"/api/v1/jobs/{first['job']['id']}")
    job = job_response.json()["job"]
    events = job_response.json()["events"]
    assert job["status"] == "succeeded"
    assert job["stage"] == "completed"
    assert job["progress"] == 100
    assert {event["stage"] for event in events} >= {
        "validating",
        "extracting",
        "transcribing",
        "segmenting",
        "keywording",
        "matching",
        "persisting",
        "completed",
    }

    project_id = first["project"]["id"]
    detail = client.get(f"/api/v1/projects/{project_id}").json()
    assert detail["project"]["status"] == "ready"
    assert detail["source"]["transcript_text"] == SAMPLE_TEXT
    assert len(detail["segments"]) >= 3
    for segment in detail["segments"]:
        assert len(segment["recommendations"]) >= 3
        assert len({item["asset_id"] for item in segment["recommendations"]}) == len(
            segment["recommendations"]
        )
        assert segment["selection"]["source"] == "auto"
        assert segment["recommendations"][0]["explanation"]
        assert set(segment["recommendations"][0]) >= {
            "tfidf_score",
            "keyword_score",
            "tag_score",
            "matched_terms",
        }

    target = detail["segments"][0]
    edited = client.patch(
        f"/api/v1/segments/{target['id']}",
        json={
            "text": "网络安全团队通过风险评估保护关键数据和用户隐私。",
            "topic": "数据安全",
            "keywords": ["网络安全", "数据", "隐私"],
            "version": target["version"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == target["version"] + 1
    assert edited.json()["recommendations"][0]["asset_id"] == "seed-security"

    conflict = client.patch(
        f"/api/v1/segments/{target['id']}",
        json={"text": "过期写入", "version": target["version"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "SEGMENT_VERSION_CONFLICT"

    replacement = edited.json()["recommendations"][1]["asset_id"]
    selected = client.put(
        f"/api/v1/segments/{target['id']}/selection", json={"asset_id": replacement}
    )
    assert selected.status_code == 200
    assert selected.json()["asset_id"] == replacement
    assert selected.json()["source"] == "manual"

    rematched = client.post(f"/api/v1/segments/{target['id']}/rematch")
    assert rematched.status_code == 200
    assert rematched.json()["selection"]["asset_id"] == replacement
    assert rematched.json()["selection"]["source"] == "manual"

    before_order = [item["id"] for item in detail["segments"]]
    reversed_order = list(reversed(before_order))
    reordered = client.put(
        f"/api/v1/projects/{project_id}/segments/order", json={"segment_ids": reversed_order}
    )
    assert reordered.status_code == 200, reordered.text
    assert [item["id"] for item in reordered.json()["segments"]] == reversed_order

    refreshed = client.get(f"/api/v1/projects/{project_id}").json()
    assert [item["id"] for item in refreshed["segments"]] == reversed_order
    refreshed_target = next(item for item in refreshed["segments"] if item["id"] == target["id"])
    assert refreshed_target["text"].startswith("网络安全团队")
    assert refreshed_target["selection"]["asset_id"] == replacement
    assert refreshed_target["selection"]["source"] == "manual"

    assets = client.get("/api/v1/assets?q=安全").json()
    assert assets["total"] >= 1
    assert any(item["id"] == "seed-security" for item in assets["items"])
    assert client.get("/api/v1/assets").json()["total"] >= 12
    assert client.get("/api/v1/runs").json()["total"] >= 2
    actions = {
        item["action"]
        for item in client.get(f"/api/v1/audit?project_id={project_id}").json()["items"]
    }
    assert {
        "project.created",
        "job.succeeded",
        "segment.updated",
        "selection.changed",
        "segments.reordered",
    } <= actions


def test_fault_failure_manual_retry_then_success(runtime):
    client, worker, _database, _settings = runtime
    fault = client.post("/api/v1/demo/faults/next", json={"mode": "job_fail"})
    assert fault.status_code == 200
    created = create_project(client, title="故障重试", key="fault-key")
    worker.run_once()
    failed = client.get(f"/api/v1/jobs/{created['job']['id']}").json()
    assert failed["job"]["status"] == "failed"
    assert failed["job"]["error_code"] == "DEMO_JOB_FAILURE"
    assert failed["job"]["retryable"] is True

    retry = client.post(f"/api/v1/jobs/{created['job']['id']}/retry")
    assert retry.status_code == 202
    assert retry.json()["job"]["status"] == "queued"
    duplicate_retry = client.post(f"/api/v1/jobs/{created['job']['id']}/retry")
    assert duplicate_retry.status_code == 409
    assert duplicate_retry.json()["code"] == "INVALID_STATE"
    worker.run_once()
    succeeded = client.get(f"/api/v1/jobs/{created['job']['id']}").json()
    assert succeeded["job"]["status"] == "succeeded"
    assert succeeded["job"]["attempt"] == 2


def test_asr_configuration_fix_retries_same_project_without_reupload(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.asr_provider = "openai"
    settings.openai_api_key = None
    created_response = client.post(
        "/api/v1/projects/upload",
        data={"title": "ASR 配置修复后重试"},
        files={"file": ("config-retry.wav", b"RIFF\x04\x00\x00\x00WAVE", "audio/wav")},
        headers={"Idempotency-Key": "asr-config-retry"},
    )
    assert created_response.status_code == 202, created_response.text
    created = created_response.json()
    project_id = created["project"]["id"]
    job_id = created["job"]["id"]

    with database.session() as session:
        original_source = session.scalar(select(Source).where(Source.project_id == project_id))
        original_source_id = original_source.id
        original_storage_path = original_source.storage_path
        original_sha256 = original_source.sha256

    assert worker.run_once() is True
    failed = client.get(f"/api/v1/jobs/{job_id}").json()
    assert failed["job"]["status"] == "failed"
    assert failed["job"]["attempt"] == 1
    assert failed["job"]["error_code"] == "ASR_OPENAI_KEY_MISSING"
    assert failed["job"]["retryable"] is True
    assert any(
        "配置错误 / ASR_OPENAI_KEY_MISSING" in event["message"]
        for event in failed["events"]
    )
    failed_event_ids = {event["id"] for event in failed["events"]}

    settings.openai_api_key = "fixed-test-key"

    def successful_provider_transcribe(_path, _mime_type, active_settings):
        assert active_settings.openai_api_key == "fixed-test-key"
        return SAMPLE_TEXT, "openai-compatible/test-asr"

    monkeypatch.setattr("app.asr._openai_transcribe", successful_provider_transcribe)
    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 202, retry.text
    retried = retry.json()
    assert retried["job"]["id"] == job_id
    assert retried["job"]["project_id"] == project_id
    assert retried["job"]["status"] == "queued"
    assert retried["job"]["attempt"] == 1
    assert retried["job"]["error_code"] == "ASR_OPENAI_KEY_MISSING"
    assert failed_event_ids < {event["id"] for event in retried["events"]}
    assert any("已请求重新执行原任务" in event["message"] for event in retried["events"])

    assert worker.run_once() is True
    succeeded = client.get(f"/api/v1/jobs/{job_id}").json()
    assert succeeded["job"]["status"] == "succeeded"
    assert succeeded["job"]["attempt"] == 2
    assert succeeded["job"]["error_code"] is None
    assert succeeded["job"]["error_message"] is None
    assert failed_event_ids < {event["id"] for event in succeeded["events"]}
    assert any("ASR_OPENAI_KEY_MISSING" in event["message"] for event in succeeded["events"])
    assert any(event["stage"] == "completed" for event in succeeded["events"])
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409

    with database.session() as session:
        source = session.scalar(select(Source).where(Source.project_id == project_id))
        assert source.id == original_source_id
        assert source.storage_path == original_storage_path
        assert source.sha256 == original_sha256
        assert session.scalar(select(func.count()).select_from(Project)) == 1


def test_ai_degrade_succeeds_and_is_traceable(runtime):
    client, worker, _database, _settings = runtime
    client.post("/api/v1/demo/faults/next", json={"mode": "ai_degrade"})
    created = create_project(client, title="规则降级", key="degrade-key")
    worker.run_once()
    detail = client.get(f"/api/v1/projects/{created['project']['id']}").json()
    assert detail["project"]["status"] == "ready"
    assert detail["trace_summary"]["degraded"] is True
    events = client.get(f"/api/v1/jobs/{created['job']['id']}").json()["events"]
    assert any(event["level"] == "warning" and "规则" in event["message"] for event in events)
    runs = client.get("/api/v1/runs").json()["items"]
    assert any(run["degraded"] is True for run in runs)


def test_subtitle_upload_is_real_async_input_and_cancel_is_persisted(runtime):
    client, worker, _database, _settings = runtime
    upload = client.post(
        "/api/v1/projects/upload",
        data={"title": "字幕上传"},
        files={
            "file": (
                "demo.srt",
                "1\n00:00:00,000 --> 00:00:03,000\n阅读带来知识与成长。\n\n2\n00:00:03,000 --> 00:00:06,000\n团队协作创造更多可能。",
                "application/x-subrip",
            )
        },
    )
    assert upload.status_code == 202, upload.text
    worker.run_once()
    detail = client.get(f"/api/v1/projects/{upload.json()['project']['id']}").json()
    assert detail["project"]["status"] == "ready"
    assert "阅读带来知识" in detail["source"]["transcript_text"]

    queued = create_project(client, title="取消任务", key="cancel-key")
    canceled = client.post(f"/api/v1/jobs/{queued['job']['id']}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["job"]["status"] == "canceled"
    assert worker.run_once() is False
    assert client.post(f"/api/v1/jobs/{queued['job']['id']}/retry").status_code == 409
    persisted = client.get(f"/api/v1/projects/{queued['project']['id']}").json()
    assert persisted["project"]["status"] == "canceled"


def test_errors_and_health_use_stable_contract(runtime):
    client, worker, _database, _settings = runtime
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["code"] == "NOT_READY"
    assert ready.json()["details"]["checks"]["seed_assets"]["count"] >= 12
    worker.heartbeat()
    ready_response = client.get("/api/v1/health/ready")
    assert ready_response.status_code == 200
    ready_after = ready_response.json()
    assert ready_after["checks"]["worker"]["online"] is True

    missing = client.get("/api/v1/projects/not-found")
    assert missing.status_code == 404
    assert set(missing.json()) >= {"code", "message", "retryable", "request_id"}
    invalid = client.post("/api/v1/projects/text", json={"title": "", "text": "x"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_expired_running_lease_is_recovered_after_worker_restart(runtime):
    client, worker, database, _settings = runtime
    created = create_project(client, title="租约恢复", key="lease-key")
    with database.session() as session:
        job = session.get(Job, created["job"]["id"])
        project = session.get(Project, created["project"]["id"])
        job.status = "running"
        job.lease_owner = "crashed-worker"
        job.lease_expires_at = utcnow() - timedelta(seconds=10)
        job.progress = 46
        project.status = "processing"
        session.add(
            JobEvent(
                job_id=job.id,
                stage="matching",
                progress=80,
                message="崩溃前已经到达匹配阶段",
            )
        )
    assert worker.run_once() is True
    result = client.get(f"/api/v1/jobs/{created['job']['id']}").json()
    assert result["job"]["status"] == "succeeded"
    assert any("过期租约" in event["message"] for event in result["events"])
    progresses = [event["progress"] for event in result["events"]]
    assert progresses == sorted(progresses)


def test_retry_rejects_permanent_failure_and_attempt_exhaustion(runtime):
    client, _worker, database, _settings = runtime
    running = create_project(client, title="正在执行", key="running-key")
    with database.session() as session:
        job = session.get(Job, running["job"]["id"])
        job.status = "running"
        job.attempt = 1
        session.get(Project, job.project_id).status = "processing"
    response = client.post(f"/api/v1/jobs/{running['job']['id']}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_STATE"
    with database.session() as session:
        job = session.get(Job, running["job"]["id"])
        assert job.status == "running"
        assert job.attempt == 1

    repaired_config = create_project(client, title="配置修复后再执行", key="rearm-config-key")
    with database.session() as session:
        job = session.get(Job, repaired_config["job"]["id"])
        job.status = "failed"
        job.retryable = False
        job.error_code = "ASR_MODEL_DOWNLOAD_NETWORK_ERROR"
        job.error_message = "Whisper 模型下载失败"
        job.attempt = job.max_attempts
        session.get(Project, job.project_id).status = "failed"
        previous_max_attempts = job.max_attempts
    repairable_detail = client.get(f"/api/v1/jobs/{repaired_config['job']['id']}").json()["job"]
    assert repairable_detail["retryable"] is True
    assert repairable_detail["max_attempts"] == previous_max_attempts + 1
    response = client.post(f"/api/v1/jobs/{repaired_config['job']['id']}/retry")
    assert response.status_code == 202
    assert response.json()["job"]["status"] == "queued"
    assert response.json()["job"]["max_attempts"] == previous_max_attempts + 1

    permanent = create_project(client, title="不可重试", key="permanent-key")
    with database.session() as session:
        job = session.get(Job, permanent["job"]["id"])
        job.status = "failed"
        job.retryable = False
        job.error_code = "PERMANENT"
        session.get(Project, job.project_id).status = "failed"
    response = client.post(f"/api/v1/jobs/{permanent['job']['id']}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "JOB_NOT_RETRYABLE"

    exhausted = create_project(client, title="次数耗尽", key="exhausted-key")
    with database.session() as session:
        job = session.get(Job, exhausted["job"]["id"])
        job.status = "failed"
        job.retryable = True
        job.attempt = job.max_attempts
        session.get(Project, job.project_id).status = "failed"
    response = client.post(f"/api/v1/jobs/{exhausted['job']['id']}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "JOB_ATTEMPTS_EXHAUSTED"
    with database.session() as session:
        job = session.get(Job, exhausted["job"]["id"])
        assert job.max_attempts == 3


def test_stale_worker_cannot_fail_a_recovered_lease(runtime):
    client, stale_worker, database, settings = runtime
    created = create_project(client, title="租约 fencing", key="fencing-key")
    job_id = created["job"]["id"]
    assert stale_worker.claim() == job_id
    with database.session() as session:
        job = session.get(Job, job_id)
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    recovered_worker = DurableWorker(database, settings, worker_id="recovered-worker")
    assert recovered_worker.claim() == job_id

    stale_generation = stale_worker._claimed_generations[job_id]
    stale_worker._fail(
        job_id, stale_generation, WorkerFailure("STALE_FAILURE", "stale worker", True)
    )

    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.status == "running"
        assert job.lease_owner == "recovered-worker"
        assert job.error_code is None


def test_asset_upload_patch_dashboard_and_project_delete(runtime):
    client, worker, _database, _settings = runtime
    # A real 1x1 PNG, not extension-only spoofing.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    uploaded = client.post(
        "/api/v1/assets",
        data={"name": "自定义产品图", "tags": "产品,办公", "keywords": "界面,效率"},
        files={"file": ("product.png", png, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["id"]
    media = client.get(uploaded.json()["url"])
    assert media.status_code == 200
    assert media.content.startswith(b"\x89PNG")

    auto_tagged = client.post(
        "/api/v1/assets",
        data={"name": "城市通勤自动标签素材", "tags": "", "keywords": ""},
        files={"file": ("auto-tags.png", png, "image/png")},
    )
    assert auto_tagged.status_code == 201, auto_tagged.text
    assert auto_tagged.json()["tags"]
    assert auto_tagged.json()["keywords"]
    tagging_run = next(
        item
        for item in client.get("/api/v1/runs").json()["items"]
        if item["operation"] == "asset_tagging"
    )
    assert tagging_run["prompt_version"] == "asset-tags-v1"
    assert tagging_run["input_hash"]
    assert tagging_run["provider"] == "rules"

    patched = client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"name": "产品效率界面", "tags": ["产品", "效率"], "keywords": ["工作台"]},
    )
    assert patched.status_code == 200
    assert patched.json()["keywords"] == ["工作台"]
    assert any(item["id"] == asset_id for item in client.get("/api/v1/assets?q=工作台").json()["items"])

    spoofed = client.post(
        "/api/v1/assets",
        data={"name": "伪造图片", "tags": "", "keywords": ""},
        files={"file": ("bad.png", b"<script>alert(1)</script>", "image/png")},
    )
    assert spoofed.status_code == 415
    assert spoofed.json()["code"] == "ASSET_SIGNATURE_MISMATCH"

    created = create_project(client, title="待删除项目", key="delete-key")
    worker.run_once()
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["metrics"]["projects"] == 1
    assert dashboard["metrics"]["total_assets"] >= 13
    deleted = client.delete(f"/api/v1/projects/{created['project']['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/projects/{created['project']['id']}").status_code == 404
    recreated = create_project(client, title="幂等记录已随项目清理", key="delete-key")
    assert recreated["project"]["id"] != created["project"]["id"]


def test_pipeline_records_semantic_and_matching_runs_separately(runtime):
    client, worker, _database, _settings = runtime
    created = create_project(client, title="AI 匹配追踪", key="trace-runs")
    assert worker.run_once() is True
    runs = client.get("/api/v1/runs").json()["items"]
    semantic = next(item for item in runs if item["operation"] == "semantic_segmentation")
    matching = next(item for item in runs if item["operation"] == "asset_matching")
    assert semantic["job_id"] == created["job"]["id"]
    assert semantic["prompt_version"] == "semantic-segments-v1"
    assert matching["job_id"] == created["job"]["id"]
    assert matching["prompt_version"] == "hybrid-ranker-v2"
    assert matching["input_hash"]
    assert matching["output_summary"]["traces"]


def test_audio_pipeline_records_dashscope_transcription_run(runtime, monkeypatch):
    client, worker, _database, _settings = runtime

    monkeypatch.setattr(
        "app.worker.transcribe_file",
        lambda _path, _mime_type, _settings: (
            "阿里云百炼语音识别输出会继续进入字幕语义增强与素材匹配。",
            "dashscope/paraformer-v2",
        ),
    )
    response = client.post(
        "/api/v1/projects/upload",
        data={"title": "DashScope 转写追踪"},
        files={"file": ("trace.wav", b"RIFF\x04\x00\x00\x00WAVE", "audio/wav")},
        headers={"Idempotency-Key": "dashscope-trace-run"},
    )
    assert response.status_code == 202, response.text
    created = response.json()
    assert worker.run_once() is True

    transcription = next(
        item
        for item in client.get("/api/v1/runs").json()["items"]
        if item["operation"] == "speech_transcription"
        and item["job_id"] == created["job"]["id"]
    )
    assert transcription["provider"] == "dashscope"
    assert transcription["model"] == "paraformer-v2"
    assert transcription["prompt_version"] == "speech-transcription-v1"
    assert transcription["status"] == "succeeded"
    assert transcription["output_summary"]["source_kind"] == "audio"
    assert transcription["output_summary"]["characters"] > 0


def test_matching_run_records_actual_embedding_provider(runtime):
    client, worker, _database, _settings = runtime

    class FakeEmbedding:
        name = "fake-embedding"
        provider = "test-vector-provider"
        model = "test-vector-model"

        def cosine_scores(self, _query, documents):
            return [0.5 for _ in documents]

    worker._semantic_scorer = FakeEmbedding()
    created = create_project(client, title="向量追踪", key="vector-trace-provider")
    assert worker.run_once() is True
    matching = next(
        item
        for item in client.get("/api/v1/runs").json()["items"]
        if item["operation"] == "asset_matching" and item["job_id"] == created["job"]["id"]
    )
    assert matching["provider"] == "test-vector-provider"
    assert matching["model"] == "test-vector-model"
    assert all(
        trace["provider"] == "test-vector-provider"
        for trace in matching["output_summary"]["traces"]
    )


def test_expired_running_lease_stops_after_max_attempts(runtime):
    client, worker, database, _settings = runtime
    created = create_project(client, title="恢复次数耗尽", key="exhausted-lease")
    with database.session() as session:
        job = session.get(Job, created["job"]["id"])
        job.status = "running"
        job.attempt = job.max_attempts
        job.lease_owner = "crashed-worker"
        job.lease_expires_at = utcnow() - timedelta(seconds=10)
        session.get(Project, job.project_id).status = "processing"
    assert worker.claim() is None
    with database.session() as session:
        job = session.get(Job, created["job"]["id"])
        assert job.status == "failed"
        assert job.retryable is False
        assert job.error_code == "JOB_ATTEMPTS_EXHAUSTED"
        assert job.lease_owner is None


def test_subtitle_limit_and_private_source_storage(runtime):
    client, _worker, database, settings = runtime
    settings.max_subtitle_chars = 20
    rejected = client.post(
        "/api/v1/projects/upload",
        data={"title": "超长字幕"},
        files={"file": ("long.srt", ("1\n00:00:00,000 --> 00:00:01,000\n" + "字" * 21).encode(), "text/plain")},
    )
    assert rejected.status_code == 413
    assert rejected.json()["code"] == "SUBTITLE_TOO_LONG"
    settings.max_subtitle_chars = 200_000
    accepted = client.post(
        "/api/v1/projects/upload",
        data={"title": "私有字幕"},
        files={"file": ("private.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello", "text/plain")},
    )
    assert accepted.status_code == 202
    with database.session() as session:
        source = session.scalar(select(Source).where(Source.project_id == accepted.json()["project"]["id"]))
        assert source.public_url is None
        assert Path(source.storage_path).parent == settings.data_dir / "private" / "sources"
