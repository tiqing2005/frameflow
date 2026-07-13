from __future__ import annotations

from fastapi import APIRouter, Request, status

from ..errors import request_id
from ..schemas import PreviewCreate
from ..services import create_preview_job, get_project_preview, public_preview_plan
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["previews"])


@router.get("/projects/{project_id}/timeline")
def get_timeline(project_id: str, session: SessionDep):
    return public_preview_plan(session, project_id)


@router.get("/projects/{project_id}/preview")
def get_preview(project_id: str, session: SessionDep):
    return get_project_preview(session, project_id)


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
