from __future__ import annotations

from fastapi import APIRouter, Query

from ..services import list_audit
from ._deps import SessionDep

router = APIRouter(prefix="/api/v1", tags=["trace"])


@router.get("/audit")
def get_audit(
    session: SessionDep,
    project_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return list_audit(session, project_id, limit, offset)
