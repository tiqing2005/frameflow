from __future__ import annotations

import threading

from sqlalchemy import event, select, text, update

from app.errors import APIError
from app.models import FaultControl, Job, utcnow
from app.services.jobs import cancel_job
from app.worker import DurableWorker


def create_text_job(client, key: str) -> dict:
    response = client.post(
        "/api/v1/projects/text",
        json={
            "title": key,
            "text": "Concurrent task input contains enough words for durable processing.",
        },
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_sqlite_workers_claim_distinct_jobs_atomically(runtime):
    client, _worker, database, settings = runtime
    first = create_text_job(client, "concurrent-claim-first")
    second = create_text_job(client, "concurrent-claim-second")
    expected = {first["job"]["id"], second["job"]["id"]}
    workers = [
        DurableWorker(database, settings, worker_id="claim-worker-a"),
        DurableWorker(database, settings, worker_id="claim-worker-b"),
    ]
    barrier = threading.Barrier(2)
    claimed: list[str | None] = []
    errors: list[BaseException] = []

    def run_claim(worker: DurableWorker) -> None:
        try:
            barrier.wait(timeout=2)
            claimed.append(worker.claim())
        except BaseException as exc:  # surfaced in the test thread below
            errors.append(exc)

    threads = [threading.Thread(target=run_claim, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert set(claimed) == expected
    assert len(claimed) == len(set(claimed))
    with database.session() as session:
        jobs = session.scalars(select(Job).where(Job.id.in_(expected))).all()
        assert {job.lease_owner for job in jobs} == {worker.worker_id for worker in workers}
        assert all(job.status == "running" and job.attempt == 1 for job in jobs)


def test_two_workers_execute_distinct_jobs_at_the_same_time(runtime, monkeypatch):
    client, _worker, database, settings = runtime
    first = create_text_job(client, "concurrent-execution-first")
    second = create_text_job(client, "concurrent-execution-second")
    expected = {first["job"]["id"], second["job"]["id"]}
    workers = [
        DurableWorker(database, settings, worker_id="execution-worker-a"),
        DurableWorker(database, settings, worker_id="execution-worker-b"),
    ]
    both_running = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    executions: list[tuple[str, str]] = []

    def blocking_pipeline(self: DurableWorker, job_id: str, _generation: int) -> None:
        with lock:
            executions.append((self.worker_id, job_id))
            if len(executions) == 2:
                both_running.set()
        assert release.wait(5)

    monkeypatch.setattr(DurableWorker, "_process_pipeline", blocking_pipeline)
    runners = [threading.Thread(target=worker.run_once) for worker in workers]
    for runner in runners:
        runner.start()
    try:
        assert both_running.wait(5), "the second worker never entered execution concurrently"
        assert {job_id for _worker_id, job_id in executions} == expected
        assert {worker_id for worker_id, _job_id in executions} == {
            worker.worker_id for worker in workers
        }
        assert all(runner.is_alive() for runner in runners)
    finally:
        release.set()
        for runner in runners:
            runner.join(5)

    assert all(not runner.is_alive() for runner in runners)


def test_fault_control_is_consumed_by_only_one_sqlite_worker(runtime):
    _client, _worker, database, settings = runtime
    with database.session() as session:
        control = session.get(FaultControl, 1)
        if control is None:
            session.add(FaultControl(id=1, next_mode="job_fail"))
        else:
            control.next_mode = "job_fail"
            control.updated_at = utcnow()

    workers = [
        DurableWorker(database, settings, worker_id="fault-worker-a"),
        DurableWorker(database, settings, worker_id="fault-worker-b"),
    ]
    barrier = threading.Barrier(2)
    consumed: list[str] = []

    def consume(worker: DurableWorker) -> None:
        barrier.wait(timeout=2)
        consumed.append(worker._consume_fault())

    threads = [threading.Thread(target=consume, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(consumed) == ["job_fail", "none"]
    with database.session() as session:
        assert session.get(FaultControl, 1).next_mode == "none"


def test_demo_disabled_clears_and_ignores_stale_fault(runtime):
    _client, worker, database, settings = runtime
    with database.session() as session:
        control = session.get(FaultControl, 1)
        if control is None:
            session.add(FaultControl(id=1, next_mode="job_fail"))
        else:
            control.next_mode = "job_fail"
    settings.demo_mode = False

    assert worker._consume_fault() == "none"
    with database.session() as session:
        assert session.get(FaultControl, 1).next_mode == "none"


def test_cancel_compare_and_swap_does_not_overwrite_success(runtime):
    client, _worker, database, _settings = runtime
    created = create_text_job(client, "cancel-success-race")
    job_id = created["job"]["id"]
    completion_has_lock = threading.Event()
    release_completion = threading.Event()
    cancel_reached_lock = threading.Event()
    outcome: dict[str, object] = {}

    def complete_job() -> None:
        with database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="succeeded", finished_at=utcnow())
            )
            completion_has_lock.set()
            assert release_completion.wait(5)

    completer = threading.Thread(target=complete_job, name="test-completer")
    completer.start()
    assert completion_has_lock.wait(2)

    def observe_cancel_begin(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if threading.current_thread().name == "test-canceler" and "BEGIN IMMEDIATE" in statement:
            cancel_reached_lock.set()

    event.listen(database.engine, "before_cursor_execute", observe_cancel_begin)
    try:
        def cancel() -> None:
            try:
                with database.session() as session:
                    cancel_job(session, job_id, "cancel-race-request")
            except APIError as exc:
                outcome["status_code"] = exc.status_code
                outcome["code"] = exc.code

        canceler = threading.Thread(target=cancel, name="test-canceler")
        canceler.start()
        assert cancel_reached_lock.wait(2)
        release_completion.set()
        completer.join(5)
        canceler.join(5)
        assert not completer.is_alive()
        assert not canceler.is_alive()
    finally:
        release_completion.set()
        event.remove(database.engine, "before_cursor_execute", observe_cancel_begin)

    assert outcome == {"status_code": 409, "code": "JOB_NOT_CANCELABLE"}
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded"
        assert job.finished_at is not None
