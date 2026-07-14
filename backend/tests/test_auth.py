from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, hash_password, verify_password
from app.config import Settings
from app.main import create_app


def auth_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "auth-data"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'auth.db').as_posix()}",
        auth_enabled=True,
        auth_username="reviewer",
        auth_display_name="评审账号",
        auth_password_hash=hash_password("correct-demo-password"),
        frontend_dir=tmp_path / "missing-frontend",
    )
    return TestClient(create_app(settings))


def test_password_hash_round_trip():
    encoded = hash_password("strong-password")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("strong-password", encoded)
    assert not verify_password("wrong-password", encoded)
    assert not verify_password("strong-password", "invalid")


def test_login_session_csrf_and_logout(tmp_path: Path):
    with auth_client(tmp_path) as client:
        initial = client.get("/api/v1/auth/session")
        assert initial.status_code == 200
        assert initial.json()["authenticated"] is False
        assert initial.json()["configured"] is True
        assert client.get("/api/v1/dashboard").status_code == 401

        invalid = client.post(
            "/api/v1/auth/login",
            json={"username": "reviewer", "password": "wrong-password"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["code"] == "INVALID_CREDENTIALS"

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "reviewer", "password": "correct-demo-password"},
        )
        assert login.status_code == 200
        payload = login.json()
        assert payload["authenticated"] is True
        assert payload["user"]["display_name"] == "评审账号"
        assert payload["csrf_token"]
        assert COOKIE_NAME in client.cookies
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]

        assert client.get("/api/v1/dashboard").status_code == 200
        blocked = client.post("/api/v1/auth/logout")
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "CSRF_INVALID"

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": payload["csrf_token"]},
        )
        assert logout.status_code == 200
        assert client.get("/api/v1/dashboard").status_code == 401


def test_unconfigured_auth_returns_actionable_error(tmp_path: Path):
    data_dir = tmp_path / "unconfigured"
    app = create_app(
        Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{(data_dir / 'auth.db').as_posix()}",
            auth_enabled=True,
            frontend_dir=tmp_path / "missing-frontend",
        )
    )
    with TestClient(app) as client:
        session = client.get("/api/v1/auth/session").json()
        assert session["configured"] is False
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "x"})
        assert response.status_code == 503
        assert response.json()["code"] == "AUTH_NOT_CONFIGURED"
