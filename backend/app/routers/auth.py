from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..auth import (
    COOKIE_NAME,
    authenticate,
    create_session,
    credentials_configured,
    delete_session,
    find_session,
)
from ..errors import APIError
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_attempts: dict[str, deque[float]] = defaultdict(deque)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=512)

    @field_validator("username", mode="before")
    @classmethod
    def clean_username(cls, value: str) -> str:
        return value.strip()


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return (forwarded or (request.client.host if request.client else "unknown"))[:80]


def _check_login_limit(request: Request) -> None:
    now = time.monotonic()
    bucket = _attempts[_client_key(request)]
    while bucket and bucket[0] <= now - 300:
        bucket.popleft()
    if len(bucket) >= 5:
        raise APIError(429, "LOGIN_RATE_LIMITED", "登录尝试过多，请 5 分钟后重试", True)
    bucket.append(now)


def _payload(settings: Settings, *, authenticated: bool, csrf_token: str | None = None) -> dict:
    return {
        "auth_enabled": settings.auth_enabled,
        "configured": credentials_configured(settings),
        "authenticated": authenticated,
        "user": (
            {
                "username": settings.auth_username,
                "display_name": settings.auth_display_name,
                "role": "admin",
            }
            if authenticated and settings.auth_enabled
            else None
        ),
        "csrf_token": csrf_token,
    }


@router.get("/session")
def current_session(request: Request, response: Response, db: SessionDep, settings: SettingsDep):
    response.headers["Cache-Control"] = "no-store"
    if not settings.auth_enabled:
        return _payload(settings, authenticated=True)
    session = find_session(db, request.cookies.get(COOKIE_NAME))
    return _payload(
        settings,
        authenticated=session is not None,
        csrf_token=session.csrf_token if session else None,
    )


@router.post("/login")
def login(payload: LoginInput, request: Request, response: Response, db: SessionDep, settings: SettingsDep):
    response.headers["Cache-Control"] = "no-store"
    if not settings.auth_enabled:
        return _payload(settings, authenticated=True)
    if not credentials_configured(settings):
        raise APIError(503, "AUTH_NOT_CONFIGURED", "管理员密码尚未配置", False)
    _check_login_limit(request)
    if not authenticate(settings, payload.username, payload.password):
        raise APIError(401, "INVALID_CREDENTIALS", "用户名或密码错误", False)
    _attempts.pop(_client_key(request), None)
    session, token = create_session(db, settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return _payload(settings, authenticated=True, csrf_token=session.csrf_token)


@router.post("/logout")
def logout(request: Request, response: Response, db: SessionDep, settings: SettingsDep):
    response.headers["Cache-Control"] = "no-store"
    delete_session(db, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}
