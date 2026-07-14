from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class APIError(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    details: Any | None = None
    headers: Mapping[str, str] | None = None


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def error_payload(request: Request, code: str, message: str, retryable: bool, details=None) -> dict:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id(request),
    }
    if details is not None:
        payload["details"] = details
    return payload


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "").strip()
        request.state.request_id = incoming[:80] if incoming else str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(request, exc.code, exc.message, exc.retryable, exc.details),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_payload(request, "VALIDATION_ERROR", "请求参数校验失败", False, details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        message = "请求的资源不存在" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(request, code, message, False),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.exception("Unhandled request error request_id=%s", request_id(request))
        return JSONResponse(
            status_code=500,
            content=error_payload(request, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试", True),
        )
