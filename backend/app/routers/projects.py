from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile, status

from ..errors import request_id
from ..schemas import TextProjectCreate
from ..serializers import job_dict, project_dict
from ..services import (
    create_text_project,
    create_upload_project,
    dashboard,
    delete_project,
    list_projects,
    project_detail,
)
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["projects"])
dashboard_router = APIRouter(prefix="/api/v1", tags=["dashboard"])


@dashboard_router.get("/dashboard")
def get_dashboard(session: SessionDep):
    return dashboard(session)


@router.get("/projects")
def get_projects(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return list_projects(session, limit, offset)


@router.post("/projects/text", status_code=status.HTTP_202_ACCEPTED)
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
    return {"project": project_dict(project), "job": job_dict(job), "idempotent_replay": replay}


@router.post("/projects/upload", status_code=status.HTTP_202_ACCEPTED)
def post_upload_project(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    title: Annotated[str, Form(min_length=1, max_length=160)],
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    # Check the limit while streaming from SpooledTemporaryFile to avoid an
    # unbounded read in request handling.
    content = file.file.read(settings.max_upload_bytes + 1)
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
    return {"project": project_dict(project), "job": job_dict(job), "idempotent_replay": replay}


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: SessionDep):
    return project_detail(session, project_id)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project(project_id: str, session: SessionDep):
    delete_project(session, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
