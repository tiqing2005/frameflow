from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.worker import DurableWorker


@pytest.fixture()
def runtime(tmp_path: Path):
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'test.db').as_posix()}",
        stage_delay_seconds=0,
        worker_poll_seconds=0.01,
        job_lease_seconds=30,
        frontend_dir=tmp_path / "no-frontend",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        worker = DurableWorker(app.state.database, settings, worker_id="pytest-worker")
        yield client, worker, app.state.database, settings
    app.state.database.engine.dispose()

