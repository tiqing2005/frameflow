from __future__ import annotations

import base64
from datetime import timedelta

from app.models import Job, Project, utcnow


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


def test_create_job_result_edit_select_reorder_and_refresh(runtime):
    client, worker, _database, _settings = runtime

    first = create_project(client)
    replay = create_project(client, title="会被幂等键忽略", text="不同请求内容也不重复创建")
    assert replay["project"]["id"] == first["project"]["id"]
    assert replay["job"]["id"] == first["job"]["id"]
    assert replay["idempotent_replay"] is True
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
    worker.run_once()
    succeeded = client.get(f"/api/v1/jobs/{created['job']['id']}").json()
    assert succeeded["job"]["status"] == "succeeded"
    assert succeeded["job"]["attempt"] == 2


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
    persisted = client.get(f"/api/v1/projects/{queued['project']['id']}").json()
    assert persisted["project"]["status"] == "canceled"


def test_errors_and_health_use_stable_contract(runtime):
    client, worker, _database, _settings = runtime
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")
    assert live.status_code == ready.status_code == 200
    assert ready.json()["checks"]["seed_assets"]["count"] >= 12
    worker.heartbeat()
    ready_after = client.get("/api/v1/health/ready").json()
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
    assert worker.run_once() is True
    result = client.get(f"/api/v1/jobs/{created['job']['id']}").json()
    assert result["job"]["status"] == "succeeded"
    assert any("过期租约" in event["message"] for event in result["events"])


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
