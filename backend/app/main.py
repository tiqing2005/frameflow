from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Generator

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import __version__
from .config import Settings
from .db import Database
from .errors import APIError, install_error_handlers, request_id
from .models import Asset, JobEvent, Segment, WorkerHeartbeat
from .schemas import AssetPatch, FaultNext, SegmentOrder, SegmentPatch, SelectionPut, TextProjectCreate
from .serializers import asset_dict, event_dict
from .services import (
    cancel_job,
    create_asset,
    create_text_project,
    create_upload_project,
    dashboard,
    delete_project,
    get_job_detail,
    list_assets,
    list_audit,
    list_projects,
    list_runs,
    patch_asset,
    patch_segment,
    project_detail,
    put_selection,
    rematch_segment,
    reorder_segments,
    retry_job,
    set_fault,
)

logger = logging.getLogger("frameflow.api")


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


def get_session(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.database.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def create_app(settings: Settings | None = None) -> FastAPI:
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
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Idempotent-Replay"],
    )

    api = APIRouter(prefix="/api/v1")

    def live_payload() -> dict:
        return {
            "status": "ok",
            "service": "frameflow-api",
            "version": __version__,
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def ready_payload(session: Session) -> dict:
        session.execute(text("SELECT 1"))
        asset_count = session.scalar(
            select(func.count()).select_from(Asset).where(Asset.active.is_(True))
        ) or 0
        heartbeat = session.get(WorkerHeartbeat, 1)
        worker_online = False
        last_heartbeat = None
        if heartbeat:
            value = heartbeat.heartbeat_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()
            worker_online = age < max(15.0, settings.worker_poll_seconds * 8)
            last_heartbeat = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "status": "ready" if asset_count >= 12 else "degraded",
            "checks": {
                "database": "ok",
                "seed_assets": {"ok": asset_count >= 12, "count": asset_count},
                "worker": {"online": worker_online, "last_heartbeat": last_heartbeat},
            },
        }

    @api.get("/health/live", tags=["health"])
    def health_live():
        return live_payload()

    @api.get("/health/ready", tags=["health"])
    def health_ready(session: SessionDep):
        return ready_payload(session)

    @app.get("/health/live", include_in_schema=False)
    def root_health_live():
        return live_payload()

    @app.get("/health/ready", include_in_schema=False)
    def root_health_ready(session: SessionDep):
        return ready_payload(session)

    @api.get("/dashboard", tags=["dashboard"])
    def get_dashboard(session: SessionDep):
        return dashboard(session)

    @api.get("/projects", tags=["projects"])
    def get_projects(
        session: SessionDep,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return list_projects(session, limit, offset)

    @api.post("/projects/text", status_code=status.HTTP_202_ACCEPTED, tags=["projects"])
    def post_text_project(
        payload: TextProjectCreate,
        request: Request,
        response: Response,
        session: SessionDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        project, job, replay = create_text_project(
            session, payload.title, payload.text, idempotency_key, request_id(request)
        )
        response.headers["Idempotent-Replay"] = "true" if replay else "false"
        from .serializers import job_dict, project_dict

        return {"project": project_dict(project), "job": job_dict(job), "idempotent_replay": replay}

    @api.post("/projects/upload", status_code=status.HTTP_202_ACCEPTED, tags=["projects"])
    async def post_upload_project(
        request: Request,
        response: Response,
        session: SessionDep,
        title: Annotated[str, Form(min_length=1, max_length=160)],
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        # Check the limit while streaming from SpooledTemporaryFile to avoid an
        # unbounded read in request handling.
        content = await file.read(settings.max_upload_bytes + 1)
        project, job, replay = create_upload_project(
            session,
            settings,
            title,
            file.filename or "upload",
            file.content_type,
            content,
            idempotency_key,
            request_id(request),
        )
        response.headers["Idempotent-Replay"] = "true" if replay else "false"
        from .serializers import job_dict, project_dict

        return {"project": project_dict(project), "job": job_dict(job), "idempotent_replay": replay}

    @api.get("/projects/{project_id}", tags=["projects"])
    def get_project(project_id: str, session: SessionDep):
        return project_detail(session, project_id)

    @api.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["projects"])
    def remove_project(project_id: str, session: SessionDep):
        delete_project(session, project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str, session: SessionDep):
        return get_job_detail(session, job_id)

    @api.get("/jobs/{job_id}/events", tags=["jobs"])
    def get_job_events(job_id: str, session: SessionDep):
        detail = get_job_detail(session, job_id)
        return {"items": detail["events"], "total": len(detail["events"])}

    @api.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED, tags=["jobs"])
    def post_retry(job_id: str, request: Request, session: SessionDep):
        return retry_job(session, job_id, request_id(request))

    @api.post("/jobs/{job_id}/cancel", tags=["jobs"])
    def post_cancel(job_id: str, request: Request, session: SessionDep):
        return cancel_job(session, job_id, request_id(request))

    @api.patch("/segments/{segment_id}", tags=["segments"])
    def update_segment(segment_id: str, payload: SegmentPatch, request: Request, session: SessionDep):
        return patch_segment(session, segment_id, payload, request_id(request))

    @api.put("/projects/{project_id}/segments/order", tags=["segments"])
    def update_segment_order(
        project_id: str, payload: SegmentOrder, request: Request, session: SessionDep
    ):
        segments = reorder_segments(session, project_id, payload.segment_ids, request_id(request))
        return {"segments": segments}

    @api.get("/projects/{project_id}/segments", tags=["segments"])
    def get_project_segments(project_id: str, session: SessionDep):
        detail = project_detail(session, project_id)
        return {"items": detail["segments"], "total": len(detail["segments"])}

    @api.post("/segments/{segment_id}/rematch", tags=["segments"])
    def post_rematch(segment_id: str, request: Request, session: SessionDep):
        segment = session.get(Segment, segment_id)
        if segment is None:
            raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
        return rematch_segment(session, segment, request_id(request))

    @api.get("/segments/{segment_id}/recommendations", tags=["segments"])
    def get_recommendations(segment_id: str, session: SessionDep):
        segment = session.get(Segment, segment_id)
        if segment is None:
            raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
        detail = project_detail(session, segment.project_id)
        item = next(value for value in detail["segments"] if value["id"] == segment.id)
        return {"items": item["recommendations"], "total": len(item["recommendations"])}

    @api.put("/segments/{segment_id}/selection", tags=["segments"])
    def update_selection(
        segment_id: str, payload: SelectionPut, request: Request, session: SessionDep
    ):
        selection = put_selection(session, segment_id, payload.asset_id, request_id(request))
        return {**selection, "selection": selection}

    @api.get("/assets", tags=["assets"])
    def get_assets(
        session: SessionDep,
        q: str | None = Query(None, max_length=100),
        kind: str | None = Query(None, pattern="^(image|video)$"),
        tag: str | None = Query(None, max_length=60),
        include_inactive: bool = False,
    ):
        return list_assets(session, q, kind, tag, include_inactive)

    @api.post("/assets", status_code=status.HTTP_201_CREATED, tags=["assets"])
    async def post_asset(
        request: Request,
        session: SessionDep,
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form(min_length=1, max_length=160)],
        tags: Annotated[str, Form()] = "",
        keywords: Annotated[str, Form()] = "",
    ):
        content = await file.read(settings.max_upload_bytes + 1)
        asset = create_asset(
            session,
            settings,
            file.filename or "asset",
            file.content_type,
            content,
            name,
            tags,
            keywords,
            request_id(request),
        )
        return asset_dict(asset)

    @api.patch("/assets/{asset_id}", tags=["assets"])
    def update_asset(asset_id: str, payload: AssetPatch, request: Request, session: SessionDep):
        return asset_dict(patch_asset(session, asset_id, payload, request_id(request)))

    @api.get("/runs", tags=["trace"])
    def get_runs(
        session: SessionDep,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return list_runs(session, limit, offset)

    @api.get("/audit", tags=["trace"])
    def get_audit(
        session: SessionDep,
        project_id: str | None = None,
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        return list_audit(session, project_id, limit, offset)

    @api.post("/demo/faults/next", tags=["demo"])
    def post_demo_fault(payload: FaultNext, request: Request, session: SessionDep):
        return set_fault(session, payload.mode, request_id(request))

    app.include_router(api)
    app.mount("/media", StaticFiles(directory=settings.data_dir / "media"), name="media")

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
