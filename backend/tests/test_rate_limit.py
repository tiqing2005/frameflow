from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_api_rate_limit_separates_reads_writes_and_excludes_health(tmp_path):
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        database_url=f"sqlite:///{(data / 'rate-limit.db').as_posix()}",
        read_rate_limit_per_minute=2,
        write_rate_limit_per_minute=1,
        demo_mode=True,
        frontend_dir=tmp_path / "no-frontend",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/projects").status_code == 200
        assert client.get("/api/v1/projects").status_code == 200
        limited = client.get("/api/v1/projects")
        assert limited.status_code == 429
        assert limited.json()["code"] == "RATE_LIMITED"
        assert int(limited.headers["retry-after"]) >= 1

        assert client.post("/api/v1/demo/faults/next", json={"mode": "none"}).status_code == 200
        write_limited = client.post("/api/v1/demo/faults/next", json={"mode": "none"})
        assert write_limited.status_code == 429
        assert write_limited.json()["details"]["bucket"] == "write"

        # Probes must never be blocked by user-facing API quotas.
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/live").status_code == 200
    app.state.database.engine.dispose()
