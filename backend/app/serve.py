from __future__ import annotations

import os
import signal
import subprocess
import sys

import uvicorn


def main() -> None:
    """Run API and one durable worker in the same container lifecycle."""
    worker = subprocess.Popen([sys.executable, "-m", "app.worker"])

    def stop_worker(*_args):
        if worker.poll() is None:
            worker.terminate()

    signal.signal(signal.SIGTERM, lambda signum, frame: stop_worker())
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

