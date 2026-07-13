from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

import uvicorn

from .config import Settings
from .db import Database


def monitor_worker(worker: subprocess.Popen, stopping: threading.Event) -> None:
    """Fail fast when the colocated worker exits while the API is still serving."""
    worker.wait()
    if not stopping.is_set():
        os.kill(os.getpid(), signal.SIGTERM)


def main() -> None:
    """Run API and one durable worker in the same container lifecycle."""
    # Initialize the fresh data volume before API and worker start racing to
    # create tables and fixed-id seed rows in separate processes.
    settings = Settings.from_env()
    database = Database(settings)
    database.initialize()
    database.engine.dispose()

    worker = subprocess.Popen([sys.executable, "-m", "app.worker"])
    stopping = threading.Event()
    monitor = threading.Thread(
        target=monitor_worker,
        args=(worker, stopping),
        name="frameflow-worker-monitor",
        daemon=True,
    )
    monitor.start()

    def stop_worker(*_args):
        stopping.set()
        if worker.poll() is None:
            worker.terminate()

    def handle_term(_signum, _frame):
        stop_worker()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_term)
    try:
        uvicorn.run(
            "app.main:app",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            workers=1,
            proxy_headers=True,
        )
    finally:
        stop_worker()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()


if __name__ == "__main__":
    main()
