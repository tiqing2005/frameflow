from __future__ import annotations

from fastapi import APIRouter, Query

from ..services import list_runs
from ._deps import SessionDep

router = APIRouter(prefix="/api/v1", tags=["trace"])


@router.get("/runs")
def get_runs(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return list_runs(session, limit, offset)
