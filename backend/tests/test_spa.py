from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_spa_deep_link_falls_back_without_masking_api_404(tmp_path):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        "<!doctype html><html><body>FrameFlow SPA shell</body></html>", encoding="utf-8"
    )
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'spa.db').as_posix()}",
        frontend_dir=frontend,
        stage_delay_seconds=0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        root = client.get("/")
        deep = client.get("/projects/example-project/editor")
        missing_api = client.get("/api/v1/not-a-real-endpoint")
    assert root.status_code == deep.status_code == 200
    assert "FrameFlow SPA shell" in deep.text
    assert missing_api.status_code == 404
    assert missing_api.json()["code"] == "NOT_FOUND"
    app.state.database.engine.dispose()
