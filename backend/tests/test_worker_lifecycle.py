from __future__ import annotations

import threading
import time
from datetime import timedelta

from sqlalchemy import func, select

from app.models import AIRun, Job, Segment, Source, WorkerHeartbeat, utcnow
from app.worker import DurableWorker


def create_audio_job(client, key: str) -> dict:
    response = client.post(
        "/api/v1/projects/upload",
        data={"title": key},
        files={"file": (f"{key}.wav", b"RIFF\x04\x00\x00\x00WAVE", "audio/wav")},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202
    return response.json()


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def install_slow_asr(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_transcribe(_path, _mime_type, _settings):
        started.set()
        release.wait(5)
        return "慢任务仍然需要持续心跳，并且只能由当前执行代次写入结果。", "slow-test"

    monkeypatch.setattr("app.worker.transcribe_file", slow_transcribe)
    return started, release


def test_short_lease_slow_asr_keeps_lease_heartbeat_and_ready_busy(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.job_lease_seconds = 0.6
    settings.job_max_execution_seconds = 5
    started, release = install_slow_asr(monkeypatch)
    created = create_audio_job(client, "slow-heartbeat")
    job_id = created["job"]["id"]

    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)
    with database.session() as session:
        first_job_heartbeat = session.get(Job, job_id).heartbeat_at
        first_worker_heartbeat = session.get(WorkerHeartbeat, 1).heartbeat_at
    time.sleep(0.35)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.heartbeat_at > first_job_heartbeat
        assert (job.lease_expires_at - job.heartbeat_at).total_seconds() >= 0.5
        assert session.get(WorkerHeartbeat, 1).heartbeat_at > first_worker_heartbeat
        assert job.attempt == 1
    assert DurableWorker(database, settings, worker_id="other-worker").claim() is None
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["worker"]["state"] == "busy"
    assert ready.json()["checks"]["worker"]["current_job_id"] == job_id

    release.set()
    runner.join(3)
    assert not runner.is_alive()
    assert client.get(f"/api/v1/jobs/{job_id}").json()["job"]["status"] == "succeeded"


def test_execution_generation_change_fences_late_result(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.job_lease_seconds = 0.6
    started, release = install_slow_asr(monkeypatch)
    created = create_audio_job(client, "generation-fence")
    job_id = created["job"]["id"]
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)

    with database.session() as session:
        job = session.get(Job, job_id)
        old_generation = job.execution_generation
        job.execution_generation += 1
        assert job.lease_owner == worker.worker_id
    wait_until(lambda: not runner.is_alive())
    release.set()
    time.sleep(0.15)

    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.status == "running"
        assert job.execution_generation == old_generation + 1
        assert session.scalar(select(func.count()).select_from(Segment)) == 0
        assert session.scalar(select(func.count()).select_from(AIRun)) == 0
        assert session.scalar(select(Source.transcript_text).where(Source.project_id == job.project_id)) is None


def test_timed_out_pipeline_blocks_new_claim_until_it_unwinds(runtime, monkeypatch):
    client, worker, _database, settings = runtime
    settings.job_max_execution_seconds = 0.15
    started, release = install_slow_asr(monkeypatch)
    first = create_audio_job(client, "bounded-timeout-first")
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)
    runner.join(2)
    assert not runner.is_alive()
    assert client.get(f"/api/v1/jobs/{first['job']['id']}").json()["job"]["error_code"] == "JOB_TIMEOUT"
    second = create_audio_job(client, "bounded-timeout-second")
    assert worker.claim() is None
    release.set()
    wait_until(lambda: not next(iter(worker._timed_out_pipelines)).is_alive())
    assert worker.claim() == second["job"]["id"]


def test_job_timeout_fences_late_asr_result(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.job_lease_seconds = 0.6
    settings.job_max_execution_seconds = 0.25
    started, release = install_slow_asr(monkeypatch)
    created = create_audio_job(client, "job-timeout")
    job_id = created["job"]["id"]
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)
    runner.join(2)
    assert not runner.is_alive()

    detail = client.get(f"/api/v1/jobs/{job_id}").json()["job"]
    assert detail["status"] == "failed"
    assert detail["error_code"] == "JOB_TIMEOUT"
    release.set()
    time.sleep(0.15)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.lease_owner is None
        assert job.lease_expires_at is None
        assert session.scalar(select(func.count()).select_from(Segment)) == 0


def test_cancel_running_job_stops_renewal_and_discards_late_result(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.job_lease_seconds = 0.6
    started, release = install_slow_asr(monkeypatch)
    created = create_audio_job(client, "cancel-running")
    job_id = created["job"]["id"]
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)
    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 200
    wait_until(lambda: not runner.is_alive())
    release.set()
    time.sleep(0.15)

    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.status == "canceled"
        assert job.lease_owner is None
        assert job.lease_expires_at is None
        assert session.scalar(select(func.count()).select_from(Segment)) == 0


def test_ready_reports_dead_instead_of_busy_for_stale_worker(runtime):
    client, worker, database, _settings = runtime
    worker.heartbeat()
    with database.session() as session:
        heartbeat = session.get(WorkerHeartbeat, 1)
        heartbeat.heartbeat_at = utcnow() - timedelta(minutes=1)
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    worker_check = response.json()["details"]["checks"]["worker"]
    assert worker_check["online"] is False
    assert worker_check["state"] == "dead"
    assert worker_check["current_job_id"] is None


def test_worker_stop_abandons_lease_and_fences_late_result(runtime, monkeypatch):
    client, worker, database, settings = runtime
    settings.job_lease_seconds = 0.6
    started, release = install_slow_asr(monkeypatch)
    created = create_audio_job(client, "worker-stop")
    job_id = created["job"]["id"]
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(2)

    worker.stop()
    runner.join(2)
    assert not runner.is_alive()
    release.set()
    time.sleep(0.15)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.status == "running"
        assert job.lease_owner is None
        assert job.lease_expires_at <= job.heartbeat_at
        assert session.scalar(select(func.count()).select_from(Segment)) == 0
