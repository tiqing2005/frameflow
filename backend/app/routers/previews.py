from __future__ import annotations

from fastapi import APIRouter, Request, status

from ..errors import request_id
from ..schemas import PreviewCreate, TimelineTimingUpdate
from ..services import (
    create_preview_job,
    get_project_preview,
    public_preview_plan,
    update_timeline_timing,
)
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["previews"])


@router.get("/projects/{project_id}/timeline")
def get_timeline(project_id: str, session: SessionDep, settings: SettingsDep):
    return public_preview_plan(session, project_id, settings.preview_max_seconds * 1_000)


@router.put("/projects/{project_id}/timeline/timing")
def put_timeline_timing(
    project_id: str,
    payload: TimelineTimingUpdate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
):
    return update_timeline_timing(
        session,
        settings,
        project_id,
        payload,
        request_id(request),
    )


@router.get("/projects/{project_id}/preview")
def get_preview(project_id: str, session: SessionDep, settings: SettingsDep):
    return get_project_preview(session, settings, project_id)


@router.post(
    "/projects/{project_id}/preview",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_preview(
    project_id: str,
    payload: PreviewCreate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
):
    return create_preview_job(
        session,
        settings,
        project_id,
        payload.force,
        request_id(request),
    )
