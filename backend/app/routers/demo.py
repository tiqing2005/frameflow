from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import request_id
from ..schemas import FaultNext
from ..services import set_fault
from ._deps import SessionDep

router = APIRouter(prefix="/api/v1", tags=["demo"])


@router.post("/demo/faults/next")
def post_demo_fault(payload: FaultNext, request: Request, session: SessionDep):
    return set_fault(session, payload.mode, request_id(request))
