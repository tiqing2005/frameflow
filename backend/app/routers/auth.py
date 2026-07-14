from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from ..auth import (
    COOKIE_NAME,
    authenticate,
    create_local_identity,
    create_session,
    credentials_configured,
    delete_session,
    find_session,
    resolve_principal,
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


class SetupInput(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def clean_setup_username(cls, value: str) -> str:
        username = str(value).strip()
        if not username or not all(character.isalnum() or character in "._-" for character in username):
            raise ValueError("用户名只能包含文字、数字、点、下划线或连字符")
        return username

    @field_validator("display_name", mode="before")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, password: str) -> str:
        categories = (
            any(character.isalpha() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
        if sum(categories) < 2:
            raise ValueError("密码至少组合使用字母、数字或符号中的两类")
        if password.casefold() in {"password123", "admin123456", "1234567890"}:
            raise ValueError("请不要使用常见弱密码")
        return password


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


def _is_loopback_request(request: Request) -> bool:
    """Authorize first-run ownership from the transport peer, not spoofable headers."""
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _setup_available(request: Request, settings: Settings, db: SessionDep) -> bool:
    return bool(
        settings.auth_enabled
        and settings.auth_local_setup_enabled
        and not credentials_configured(settings, db)
        and _is_loopback_request(request)
    )


def _payload(
    request: Request,
    settings: Settings,
    db: SessionDep,
    *,
    authenticated: bool,
    csrf_token: str | None = None,
) -> dict:
    principal = resolve_principal(db, settings)
    configured = principal is not None
    return {
        "auth_enabled": settings.auth_enabled,
        "configured": configured,
        "setup_required": settings.auth_enabled and not configured,
        "setup_available": _setup_available(request, settings, db),
        "authenticated": authenticated,
        "user": (
            {
                "username": principal.username,
                "display_name": principal.display_name,
                "role": "admin",
            }
            if authenticated and settings.auth_enabled and principal
            else None
        ),
        "csrf_token": csrf_token,
    }


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )


@router.get("/session")
def current_session(request: Request, response: Response, db: SessionDep, settings: SettingsDep):
    response.headers["Cache-Control"] = "no-store"
    if not settings.auth_enabled:
        return _payload(request, settings, db, authenticated=True)
    session = find_session(db, request.cookies.get(COOKIE_NAME))
    principal = resolve_principal(db, settings)
    if session is not None and (principal is None or session.username != principal.username):
        delete_session(db, request.cookies.get(COOKIE_NAME))
        session = None
    return _payload(
        request,
        settings,
        db,
        authenticated=session is not None,
        csrf_token=session.csrf_token if session else None,
    )


@router.post("/login")
def login(payload: LoginInput, request: Request, response: Response, db: SessionDep, settings: SettingsDep):
    response.headers["Cache-Control"] = "no-store"
    if not settings.auth_enabled:
        return _payload(request, settings, db, authenticated=True)
    if not credentials_configured(settings, db):
        raise APIError(409, "AUTH_SETUP_REQUIRED", "请先创建本机管理员账号", False)
    _check_login_limit(request)
    if not authenticate(settings, payload.username, payload.password, db=db):
        raise APIError(401, "INVALID_CREDENTIALS", "用户名或密码错误", False)
    _attempts.pop(_client_key(request), None)
    session, token = create_session(db, settings)
    _set_session_cookie(response, token, settings)
    return _payload(request, settings, db, authenticated=True, csrf_token=session.csrf_token)


@router.post("/setup")
def setup(
    payload: SetupInput,
    request: Request,
    response: Response,
    db: SessionDep,
    settings: SettingsDep,
):
    """Claim a fresh local workspace exactly once and start its first session."""
    response.headers["Cache-Control"] = "no-store"
    if credentials_configured(settings, db):
        raise APIError(409, "AUTH_ALREADY_CONFIGURED", "管理员账号已经创建，请直接登录", False)
    if not settings.auth_enabled or not settings.auth_local_setup_enabled:
        raise APIError(403, "AUTH_SETUP_DISABLED", "本机账号初始化已关闭", False)
    if not _is_loopback_request(request):
        raise APIError(
            403,
            "AUTH_SETUP_LOCAL_ONLY",
            "首次创建管理员只能在运行 FrameFlow 的本机完成",
            False,
        )
    if payload.password.casefold() == payload.username.casefold():
        raise APIError(422, "WEAK_PASSWORD", "密码不能与用户名相同", False)
    try:
        principal = create_local_identity(
            db,
            settings,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except IntegrityError as exc:
        db.rollback()
        raise APIError(409, "AUTH_ALREADY_CONFIGURED", "管理员账号已经创建，请直接登录", False) from exc
    if principal is None:
        raise APIError(409, "AUTH_ALREADY_CONFIGURED", "管理员账号已经创建，请直接登录", False)
    session, token = create_session(db, settings, principal)
    _set_session_cookie(response, token, settings)
    return _payload(request, settings, db, authenticated=True, csrf_token=session.csrf_token)


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
