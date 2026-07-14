from __future__ import annotations

import asyncio
import hmac
import json
import math
import time
import uuid
from collections import defaultdict, deque
from http.cookies import SimpleCookie

from anyio import to_thread
from sqlalchemy.orm import sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import COOKIE_NAME, find_session
from .config import Settings


class RateLimitMiddleware:
    """Small single-instance limiter for the one-process demo deployment.

    Caddy protects the site with Basic Auth and request-size limits; this layer
    caps accidental/hostile API bursts before they can fill the durable queue
    or trigger repeated provider charges. A distributed deployment should
    replace it with Redis/gateway quotas.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        read_per_minute: int = 240,
        write_per_minute: int = 60,
    ) -> None:
        self.app = app
        self.read_limit = read_per_minute
        self.write_limit = write_per_minute
        self.events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    @staticmethod
    def _client_key(scope: Scope) -> str:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        forwarded = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:80]
        client = scope.get("client")
        return str(client[0])[:80] if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if not path.startswith("/api/v1") or path.startswith("/api/v1/health"):
            await self.app(scope, receive, send)
            return
        group = "read" if method in {"GET", "HEAD", "OPTIONS"} else "write"
        limit = self.read_limit if group == "read" else self.write_limit
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        window_start = now - 60.0
        key = (self._client_key(scope), group)
        async with self.lock:
            bucket = self.events[key]
            while bucket and bucket[0] <= window_start:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, math.ceil(60.0 - (now - bucket[0])))
            else:
                bucket.append(now)
                retry_after = 0

        if retry_after:
            request_id = f"req_{uuid.uuid4().hex}"
            body = json.dumps(
                {
                    "code": "RATE_LIMITED",
                    "message": "请求过于频繁，请稍后重试",
                    "retryable": True,
                    "request_id": request_id,
                    "details": {"retry_after_seconds": retry_after, "bucket": group},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            headers = [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(retry_after).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class AuthMiddleware:
    """Protect application APIs and media with an HttpOnly database session."""

    _PUBLIC_PATHS = {
        "/api/v1/auth/session",
        "/api/v1/auth/login",
        "/health/live",
        "/health/ready",
        "/api/v1/health/live",
        "/api/v1/health/ready",
    }

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        session_factory: sessionmaker,
    ) -> None:
        self.app = app
        self.settings = settings
        self.session_factory = session_factory

    @staticmethod
    def _cookie(scope: Scope, name: str) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() != b"cookie":
                continue
            cookies = SimpleCookie()
            try:
                cookies.load(value.decode("latin-1"))
                morsel = cookies.get(name)
                return morsel.value if morsel else None
            except Exception:
                return None
        return None

    @staticmethod
    def _header(scope: Scope, name: bytes) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                return value.decode("latin-1")
        return None

    @staticmethod
    async def _reject(send: Send, *, status: int, code: str, message: str) -> None:
        request_id = f"req_{uuid.uuid4().hex}"
        body = json.dumps(
            {
                "code": code,
                "message": message,
                "retryable": False,
                "request_id": request_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-request-id", request_id.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _load_session(self, token: str | None):
        db = self.session_factory()
        try:
            return find_session(db, token)
        finally:
            db.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if (
            method == "OPTIONS"
            or path in self._PUBLIC_PATHS
            or path.startswith("/api/v1/asr/source/")
        ):
            await self.app(scope, receive, send)
            return
        if not path.startswith(("/api/", "/media/")):
            await self.app(scope, receive, send)
            return

        auth_session = await to_thread.run_sync(
            self._load_session, self._cookie(scope, COOKIE_NAME)
        )
        if auth_session is None:
            await self._reject(send, status=401, code="AUTH_REQUIRED", message="请登录后继续操作")
            return
        if method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = self._header(scope, b"x-csrf-token")
            if not csrf or not hmac.compare_digest(csrf, auth_session.csrf_token):
                await self._reject(send, status=403, code="CSRF_INVALID", message="会话校验失败，请刷新后重试")
                return
        scope.setdefault("state", {})["auth_username"] = auth_session.username
        await self.app(scope, receive, send)
