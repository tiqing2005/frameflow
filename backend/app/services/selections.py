from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..errors import APIError
from ..models import Asset, Segment, Selection, utcnow
from ..serializers import selection_dict
from .common import _get_asset, _get_segment, add_audit


def put_selection(
    session: Session, segment_id: str, asset_id: str, request_id: str | None
) -> dict:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # Pair with asset deactivation's write lock to preserve the invariant
        # that every Selection references an active Asset under concurrency.
        session.execute(text("BEGIN IMMEDIATE"))
    if dialect == "postgresql":
        # The segment row is the project-timeline mutex shared with timing
        # updates, so an input-hash check cannot race a selection change.
        segment = session.scalar(
            select(Segment).where(Segment.id == segment_id).with_for_update()
        )
        if segment is None:
            raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
        asset = session.scalar(
            select(Asset).where(Asset.id == asset_id).with_for_update()
        )
        if asset is None or not asset.active:
            raise APIError(404, "ASSET_NOT_FOUND", "素材不存在或已停用")
    else:
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
