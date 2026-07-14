from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

import uvicorn

from .config import Settings
from .db import Database


def monitor_worker(worker: subprocess.Popen, stopping: threading.Event) -> None:
    """Fail fast when any colocated worker exits while the API is serving."""
    worker.wait()
    if not stopping.is_set():
        os.kill(os.getpid(), signal.SIGTERM)


def terminate_workers(workers: Sequence[subprocess.Popen]) -> None:
    """Ask all live workers to stop without blocking the API signal handler."""
    for worker in workers:
        if worker.poll() is None:
            try:
                worker.terminate()
            except OSError:
                # The worker may have exited between poll() and terminate().
                pass


def wait_for_workers(
    workers: Sequence[subprocess.Popen], timeout: float = 10.0
) -> None:
    """Reap a worker pool, killing only processes that miss the shared deadline."""
    deadline = time.monotonic() + timeout
    for worker in workers:
        if worker.poll() is not None:
            continue
        try:
            worker.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                worker.kill()
                worker.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass


def main() -> None:
    """Run the API and a bounded durable-worker process pool together."""
    # Initialize the fresh data volume before API and worker start racing to
    # create tables and fixed-id seed rows in separate processes.
    settings = Settings.from_env()
    database = Database(settings)
    database.initialize()
    database.engine.dispose()

    stopping = threading.Event()
    workers: list[subprocess.Popen] = []

    def stop_workers(*_args):
        stopping.set()
        terminate_workers(workers)

    def handle_term(_signum, _frame):
        stop_workers()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, handle_term)
        for slot in range(settings.worker_concurrency):
            worker_env = os.environ.copy()
            worker_env["FRAMEFLOW_WORKER_ID"] = f"{socket.gethostname()}:{slot + 1}"
            worker_env["FRAMEFLOW_DATABASE_INITIALIZED"] = "1"
            worker = subprocess.Popen(
                [sys.executable, "-m", "app.worker"], env=worker_env
            )
            workers.append(worker)
            monitor = threading.Thread(
                target=monitor_worker,
                args=(worker, stopping),
                name=f"frameflow-worker-monitor-{slot + 1}",
                daemon=True,
            )
            monitor.start()

        uvicorn.run(
            "app.main:app",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            workers=1,
            proxy_headers=True,
        )
    finally:
        stop_workers()
        wait_for_workers(workers)


if __name__ == "__main__":
    main()
