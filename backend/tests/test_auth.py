from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import COOKIE_NAME, hash_password, verify_password
from app.config import Settings
from app.main import create_app
from app.models import AuthIdentity


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


def test_first_run_setup_persists_hashed_admin_and_cannot_be_reclaimed(tmp_path: Path):
    data_dir = tmp_path / "unconfigured"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'auth.db').as_posix()}",
        auth_enabled=True,
        frontend_dir=tmp_path / "missing-frontend",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        session = client.get("/api/v1/auth/session").json()
        assert session["configured"] is False
        assert session["setup_required"] is True
        assert session["setup_available"] is True
        assert client.get("/api/v1/dashboard").status_code == 401

        login_before_setup = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "not-ready"}
        )
        assert login_before_setup.status_code == 409
        assert login_before_setup.json()["code"] == "AUTH_SETUP_REQUIRED"

        weak = client.post(
            "/api/v1/auth/setup",
            json={"username": "admin", "display_name": "本机管理员", "password": "1234567890"},
        )
        assert weak.status_code == 422

        created = client.post(
            "/api/v1/auth/setup",
            json={
                "username": "reviewer",
                "display_name": "本机评审账号",
                "password": "FirstRun-demo-2026",
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["configured"] is True
        assert payload["setup_required"] is False
        assert payload["setup_available"] is False
        assert payload["authenticated"] is True
        assert payload["user"]["display_name"] == "本机评审账号"
        assert COOKIE_NAME in client.cookies
        assert client.get("/api/v1/dashboard").status_code == 200

        with app.state.database.session() as db:
            identity = db.scalar(select(AuthIdentity))
            assert identity is not None
            assert identity.password_hash != "FirstRun-demo-2026"
            assert verify_password("FirstRun-demo-2026", identity.password_hash)

        second_setup = client.post(
            "/api/v1/auth/setup",
            json={
                "username": "attacker",
                "display_name": "覆盖账号",
                "password": "Another-demo-2026",
            },
        )
        assert second_setup.status_code == 409
        assert second_setup.json()["code"] == "AUTH_ALREADY_CONFIGURED"

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": payload["csrf_token"]},
        )
        assert logout.status_code == 200
        relogin = client.post(
            "/api/v1/auth/login",
            json={"username": "reviewer", "password": "FirstRun-demo-2026"},
        )
        assert relogin.status_code == 200


def test_first_run_setup_rejects_non_loopback_claim(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "remote-setup"
    app = create_app(
        Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{(data_dir / 'auth.db').as_posix()}",
            auth_enabled=True,
            frontend_dir=tmp_path / "missing-frontend",
        )
    )
    monkeypatch.setattr("app.routers.auth._is_loopback_request", lambda _request: False)
    with TestClient(app) as client:
        session = client.get("/api/v1/auth/session").json()
        assert session["setup_required"] is True
        assert session["setup_available"] is False
        response = client.post(
            "/api/v1/auth/setup",
            json={
                "username": "reviewer",
                "display_name": "远程账号",
                "password": "Remote-demo-2026",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_SETUP_LOCAL_ONLY"
