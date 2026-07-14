from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import inspect, select

from app.config import Settings
from app.db import Database
from app.models import Job, WorkerHeartbeat, utcnow


def _create_text_job(client, key: str) -> str:
    response = client.post(
        "/api/v1/projects/text",
        json={"title": key, "text": "并发任务健康检查内容。" * 4},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["job"]["id"]


def test_ready_aggregates_worker_capacity_and_active_jobs(runtime):
    client, _worker, database, settings = runtime
    settings.worker_concurrency = 2
    first_job_id = _create_text_job(client, "pool-health-first")
    second_job_id = _create_text_job(client, "pool-health-second")
    now = utcnow()

    with database.session() as session:
        session.add_all(
            [
                WorkerHeartbeat(worker_id="pool-worker-1", heartbeat_at=now),
                WorkerHeartbeat(worker_id="pool-worker-2", heartbeat_at=now),
            ]
        )
        for worker_id, job_id in (
            ("pool-worker-1", first_job_id),
            ("pool-worker-2", second_job_id),
        ):
            job = session.get(Job, job_id)
            job.status = "running"
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(minutes=1)
            job.started_at = now

    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200, response.text
    worker = response.json()["checks"]["worker"]
    assert worker["online"] is True
    assert worker["state"] == "busy"
    assert worker["online_workers"] == 2
    assert set(worker["active_job_ids"]) == {first_job_id, second_job_id}
    assert worker["capacity"] == {
        "configured": 2,
        "online": 2,
        "accepting": 2,
        "busy": 2,
        "available": 0,
    }
    assert {instance["worker_id"] for instance in worker["instances"]} == {
        "pool-worker-1",
        "pool-worker-2",
    }


def test_ready_remains_available_when_one_worker_is_isolated(runtime):
    client, _worker, database, settings = runtime
    settings.worker_concurrency = 2
    now = utcnow()
    with database.session() as session:
        session.add_all(
            [
                WorkerHeartbeat(worker_id="healthy-worker", heartbeat_at=now),
                WorkerHeartbeat(
                    worker_id="isolated-worker",
                    heartbeat_at=now,
                    operational_state="isolated",
                    status_detail="execution thread is still stopping",
                ),
            ]
        )

    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200, response.text
    worker = response.json()["checks"]["worker"]
    assert worker["state"] == "degraded"
    assert worker["accepting_jobs"] is True
    assert worker["capacity"]["accepting"] == 1
    assert "execution thread" in worker["detail"]


def test_legacy_singleton_heartbeat_schema_migrates_to_worker_rows(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE worker_heartbeats (
                id INTEGER NOT NULL PRIMARY KEY,
                worker_id VARCHAR(120) NOT NULL,
                heartbeat_at DATETIME NOT NULL,
                operational_state VARCHAR(24) NOT NULL DEFAULT 'ready',
                status_detail TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO worker_heartbeats "
            "(id, worker_id, heartbeat_at, operational_state) "
            "VALUES (1, 'legacy-worker', CURRENT_TIMESTAMP, 'ready')"
        )

    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        frontend_dir=tmp_path / "no-frontend",
    )
    database = Database(settings)
    try:
        database.initialize()
        indexes = {
            index["name"]: index
            for index in inspect(database.engine).get_indexes("worker_heartbeats")
        }
        assert indexes["ix_worker_heartbeats_worker_id"]["unique"] == 1
        with database.session() as session:
            session.add(WorkerHeartbeat(worker_id="second-worker", heartbeat_at=utcnow()))
        with database.session() as session:
            assert session.scalars(select(WorkerHeartbeat)).all()
            assert {
                heartbeat.worker_id
                for heartbeat in session.scalars(select(WorkerHeartbeat)).all()
            } == {"legacy-worker", "second-worker"}
    finally:
        database.engine.dispose()
