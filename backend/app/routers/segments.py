from __future__ import annotations

from fastapi import APIRouter, Request

from ..embeddings import get_semantic_scorer
from ..errors import APIError, request_id
from ..models import Segment
from ..schemas import SegmentOrder, SegmentPatch, SegmentTimingPatch, SelectionPut
from ..services import (
    patch_segment,
    patch_segment_timing,
    project_detail,
    put_selection,
    rematch_segment,
    reorder_segments,
)
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["segments"])


@router.patch("/segments/{segment_id}")
def update_segment(
    segment_id: str, payload: SegmentPatch, request: Request, session: SessionDep, settings: SettingsDep
):
    return patch_segment(
        session, segment_id, payload, request_id(request), semantic_scorer=get_semantic_scorer(settings)
    )


@router.patch("/segments/{segment_id}/timing")
def update_segment_timing(
    segment_id: str,
    payload: SegmentTimingPatch,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
):
    return patch_segment_timing(
        session,
        settings,
        segment_id,
        payload,
        request_id(request),
    )


@router.put("/projects/{project_id}/segments/order")
def update_segment_order(
    project_id: str, payload: SegmentOrder, request: Request, session: SessionDep
):
    segments = reorder_segments(session, project_id, payload.segment_ids, request_id(request))
    return {"segments": segments}


@router.get("/projects/{project_id}/segments")
def get_project_segments(project_id: str, session: SessionDep):
    detail = project_detail(session, project_id)
    return {"items": detail["segments"], "total": len(detail["segments"])}


@router.post("/segments/{segment_id}/rematch")
def post_rematch(segment_id: str, request: Request, session: SessionDep, settings: SettingsDep):
    return rematch_segment(
        session, segment_id, request_id(request), semantic_scorer=get_semantic_scorer(settings)
    )


@router.get("/segments/{segment_id}/recommendations")
def get_recommendations(segment_id: str, session: SessionDep):
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
    detail = project_detail(session, segment.project_id)
    item = next(value for value in detail["segments"] if value["id"] == segment.id)
    return {"items": item["recommendations"], "total": len(item["recommendations"])}


@router.put("/segments/{segment_id}/selection")
def update_selection(
    segment_id: str, payload: SelectionPut, request: Request, session: SessionDep
):
    selection = put_selection(session, segment_id, payload.asset_id, request_id(request))
    return {**selection, "selection": selection}
