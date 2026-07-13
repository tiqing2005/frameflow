from __future__ import annotations

from fastapi import APIRouter, Request, status

from ..errors import request_id
from ..services import cancel_job, get_job_detail, retry_job
from ._deps import SessionDep

router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: SessionDep):
    return get_job_detail(session, job_id)


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, session: SessionDep):
    detail = get_job_detail(session, job_id)
    return {"items": detail["events"], "total": len(detail["events"])}


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def post_retry(job_id: str, request: Request, session: SessionDep):
    return retry_job(session, job_id, request_id(request))


@router.post("/jobs/{job_id}/cancel")
def post_cancel(job_id: str, request: Request, session: SessionDep):
    return cancel_job(session, job_id, request_id(request))
