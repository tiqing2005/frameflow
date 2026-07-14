from __future__ import annotations

import mimetypes

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings
from .db import Database
from .errors import install_error_handlers
from .middleware import AuthMiddleware, RateLimitMiddleware
from .routers.auth import router as auth_router
from .routers.assets import router as assets_router
from .routers.asr import router as asr_router
from .routers.audit import router as audit_router
from .routers.demo import router as demo_router
from .routers.health import root_router as health_root_router
from .routers.health import router as health_router
from .routers.jobs import router as jobs_router
from .routers.projects import dashboard_router
from .routers.projects import router as projects_router
from .routers.previews import router as previews_router
from .routers.runs import router as runs_router
from .routers.segments import router as segments_router


class SPAStaticFiles(StaticFiles):
    """Serve the SPA shell for client routes without masking API 404s."""

    async def get_response(self, path: str, scope):
        request_path = str(scope.get("path", path)).lstrip("/")
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code == 404
                and scope.get("method") in {"GET", "HEAD"}
                and not request_path.startswith(("api/", "media/"))
            ):
                return await super().get_response("index.html", scope)
            raise
        if (
            response.status_code == 404
            and scope.get("method") in {"GET", "HEAD"}
            and not request_path.startswith(("api/", "media/"))
        ):
            return await super().get_response("index.html", scope)
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    # Windows may register .svg as image/svg. Canonical media types keep video
    # and poster responses predictable across local and container deployments.
    mimetypes.add_type("image/svg+xml", ".svg", strict=True)
    mimetypes.add_type("video/mp4", ".mp4", strict=True)
    mimetypes.add_type("video/webm", ".webm", strict=True)
    mimetypes.add_type("video/quicktime", ".mov", strict=True)
    settings = settings or Settings.from_env()
    database = Database(settings)
    database.initialize()

    app = FastAPI(
        title="FrameFlow AI API",
        version=__version__,
        description="字幕语义分段、可解释素材匹配与持久化编辑 API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.database = database

    # Registration order matters: error handlers and CORS before routes.
    install_error_handlers(app)
    app.add_middleware(
        RateLimitMiddleware,
        read_per_minute=settings.read_rate_limit_per_minute,
        write_per_minute=settings.write_rate_limit_per_minute,
    )
    app.add_middleware(
        AuthMiddleware,
        settings=settings,
        session_factory=database.SessionLocal,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=settings.auth_enabled,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Idempotent-Replay"],
    )

    routers = [
        auth_router,
        health_router,
        asr_router,
        dashboard_router,
        projects_router,
        previews_router,
        jobs_router,
        segments_router,
        assets_router,
        runs_router,
        audit_router,
    ]
    if settings.demo_mode:
        routers.append(demo_router)
    for router in routers:
        app.include_router(router)
    # Root-level (non /api/v1) health checks for load balancers/probes.
    app.include_router(health_root_router)

    # Only explicitly public outputs are mounted. Original user source files are
    # kept under /data/private and never exposed by StaticFiles.
    app.mount(
        "/media/seed",
        StaticFiles(directory=settings.data_dir / "media" / "seed"),
        name="seed-media",
    )
    app.mount(
        "/media/uploads/assets",
        StaticFiles(directory=settings.data_dir / "media" / "uploads" / "assets"),
        name="uploaded-assets",
    )
    app.mount(
        "/media/previews",
        StaticFiles(directory=settings.data_dir / "media" / "previews"),
        name="preview-media",
    )

    frontend_dir = settings.frontend_dir
    if frontend_dir and frontend_dir.is_dir() and (frontend_dir / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:

        @app.get("/", include_in_schema=False)
        def root():
            return {
                "name": "FrameFlow AI",
                "version": __version__,
                "docs": "/api/docs",
                "api": "/api/v1",
            }

    return app


app = create_app()
