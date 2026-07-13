from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Selection, utcnow
from ..serializers import selection_dict
from .common import _get_asset, _get_segment, add_audit


def put_selection(
    session: Session, segment_id: str, asset_id: str, request_id: str | None
) -> dict:
    segment = _get_segment(session, segment_id)
    asset = _get_asset(session, asset_id, active_only=True)
    selection = session.scalar(select(Selection).where(Selection.segment_id == segment.id))
    before = None
    if selection:
        before = {"asset_id": selection.asset_id, "source": selection.source}
        selection.asset_id = asset.id
        selection.source = "manual"
        selection.updated_at = utcnow()
    else:
        selection = Selection(segment_id=segment.id, asset_id=asset.id, source="manual")
        session.add(selection)
    add_audit(
        session,
        segment.project_id,
        "selection",
        selection.id,
        "selection.changed",
        before=before,
        after={"asset_id": asset.id, "source": "manual"},
        request_id=request_id,
    )
    session.flush()
    return selection_dict(selection, asset)
