from __future__ import annotations

import json
from typing import Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..models import AIRun, Asset, Recommendation, Segment, Selection
from ..nlp import extract_keywords, infer_topic, rank_assets_with_trace
from ..schemas import SegmentPatch, SegmentTimingPatch
from ..serializers import recommendation_dict, segment_base_dict, selection_dict
from .common import _get_project, add_audit, dumps, stable_hash
from .previews import (
    auto_duration_ms,
    effective_duration_ms,
    normalize_frame_duration_ms,
    public_preview_plan,
)


def _begin_immediate_if_sqlite(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _locked_segment(session: Session, segment_id: str) -> Segment:
    statement = select(Segment).where(Segment.id == segment_id)
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    segment = session.scalar(statement)
    if segment is None:
        raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
    return segment


def _segment_detail(session: Session, segment: Segment) -> dict:
    data = segment_base_dict(segment)
    recommendations = session.scalars(
        select(Recommendation)
        .where(Recommendation.segment_id == segment.id)
        .order_by(Recommendation.rank)
    ).all()
    asset_ids = {item.asset_id for item in recommendations}
    selection = session.scalar(select(Selection).where(Selection.segment_id == segment.id))
    if selection:
        asset_ids.add(selection.asset_id)
    assets = {
        asset.id: asset
        for asset in session.scalars(select(Asset).where(Asset.id.in_(asset_ids or {"-"}))).all()
    }
    data["recommendations"] = [
        recommendation_dict(item, assets[item.asset_id])
        for item in recommendations
        if item.asset_id in assets
    ]
    data["selection"] = (
        selection_dict(selection, assets[selection.asset_id])
        if selection and selection.asset_id in assets
        else None
    )
    data["selected_asset"] = data["selection"]["asset"] if data["selection"] else None
    return data


def _asset_rank_payloads(session: Session) -> list[dict]:
    assets = session.scalars(
        select(Asset).where(Asset.active.is_(True)).order_by(Asset.created_at, Asset.id)
    ).all()
    return [
        {
            "id": asset.id,
            "name": asset.name,
            "tags": json.loads(asset.tags_json),
            "keywords": json.loads(asset.keywords_json),
        }
        for asset in assets
    ]


def _rematch_segment_locked(
    session: Session,
    segment: Segment,
    request_id: str | None,
    actor: str = "user",
    degraded: bool = False,
    semantic_scorer: object | None = None,
) -> dict:
    keywords = json.loads(segment.keywords_json)
    assets = _asset_rank_payloads(session)
    ranked, ranking_trace = rank_assets_with_trace(
        segment.text, segment.topic, keywords, assets, minimum=3, semantic_scorer=semantic_scorer
    )
    if len(ranked) < 3:
        raise APIError(409, "INSUFFICIENT_ASSETS", "至少需要 3 个启用素材才能重新匹配")
    input_hash = stable_hash(
        segment.text,
        segment.topic,
        dumps(keywords),
        dumps(assets),
        "hybrid-ranker-v2",
    )
    run = AIRun(
        project_id=segment.project_id,
        segment_id=segment.id,
        operation="segment_rematch",
        provider=ranking_trace.provider,
        model=ranking_trace.model,
        prompt_version="hybrid-ranker-v2",
        input_hash=input_hash,
        status="degraded" if ranking_trace.degraded or degraded else "succeeded",
        degraded=ranking_trace.degraded or degraded,
        output_summary_json=dumps(
            {
                "candidate_count": len(ranked),
                "top_score": ranked[0].total_score,
                "segment_version": segment.version,
                "weights": {"semantic": 0.55, "keyword": 0.30, "tag_topic": 0.15},
                "similarity_source": ranking_trace.source,
            }
        ),
        error_message=ranking_trace.error_message,
    )
    session.add(run)
    session.flush()
    session.execute(delete(Recommendation).where(Recommendation.segment_id == segment.id))
    session.flush()
    for item in ranked:
        session.add(
            Recommendation(
                run_id=run.id,
                segment_id=segment.id,
                asset_id=item.asset_id,
                rank=item.rank,
                total_score=item.total_score,
                tfidf_score=item.tfidf_score,
                keyword_score=item.keyword_score,
                tag_score=item.tag_score,
                matched_terms_json=dumps(item.matched_terms),
                explanation=item.explanation,
                is_diversity_filler=item.is_diversity_filler,
            )
        )
    selection = session.scalar(select(Selection).where(Selection.segment_id == segment.id))
    if selection is None:
        session.add(Selection(segment_id=segment.id, asset_id=ranked[0].asset_id, source="auto"))
    elif selection.source == "auto":
        selection.asset_id = ranked[0].asset_id
    add_audit(
        session,
        segment.project_id,
        "segment",
        segment.id,
        "segment.rematched",
        after={"run_id": run.id, "candidate_count": len(ranked)},
        actor=actor,
        request_id=request_id,
    )
    session.flush()
    return _segment_detail(session, segment)


def rematch_segment(
    session: Session,
    segment_id: str,
    request_id: str | None,
    actor: str = "user",
    degraded: bool = False,
    semantic_scorer: object | None = None,
) -> dict:
    """Rematch through the same timeline mutex used by timing mutations."""
    _begin_immediate_if_sqlite(session)
    segment = _locked_segment(session, segment_id)
    return _rematch_segment_locked(
        session,
        segment,
        request_id,
        actor=actor,
        degraded=degraded,
        semantic_scorer=semantic_scorer,
    )


def patch_segment(
    session: Session,
    segment_id: str,
    payload: SegmentPatch,
    request_id: str | None,
    semantic_scorer: object | None = None,
) -> dict:
    _begin_immediate_if_sqlite(session)
    segment = _locked_segment(session, segment_id)
    if segment.version != payload.version:
        raise APIError(
            409,
            "SEGMENT_VERSION_CONFLICT",
            "片段已被其他操作更新，请刷新后重试",
            details={"expected": segment.version, "received": payload.version},
        )
    before = segment_base_dict(segment)
    if payload.text is not None:
        segment.text = payload.text
    if payload.keywords is not None:
        segment.keywords_json = dumps(payload.keywords)
    elif payload.text is not None:
        segment.keywords_json = dumps(extract_keywords(segment.text))
    if payload.topic is not None:
        segment.topic = payload.topic
    elif payload.text is not None:
        segment.topic = infer_topic(segment.text, json.loads(segment.keywords_json))
    segment.version += 1
    session.flush()
    add_audit(
        session,
        segment.project_id,
        "segment",
        segment.id,
        "segment.updated",
        before=before,
        after=segment_base_dict(segment),
        request_id=request_id,
    )
    return _rematch_segment_locked(
        session,
        segment,
        request_id,
        actor="system",
        semantic_scorer=semantic_scorer,
    )


def patch_segment_timing(
    session: Session,
    settings: Settings,
    segment_id: str,
    payload: SegmentTimingPatch,
    request_id: str | None,
) -> dict:
    """Update visual pacing without changing transcript timing or rematching assets."""
    _begin_immediate_if_sqlite(session)

    project_id = session.scalar(select(Segment.project_id).where(Segment.id == segment_id))
    if project_id is None:
        raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
    statement = (
        select(Segment)
        .where(Segment.project_id == project_id)
        .order_by(Segment.position, Segment.id)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    segments = session.scalars(statement).all()
    segment = next((item for item in segments if item.id == segment_id), None)
    if segment is None:
        raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
    if segment.version != payload.version:
        raise APIError(
            409,
            "SEGMENT_VERSION_CONFLICT",
            "片段已被其他操作更新，请刷新后重试",
            details={"expected": segment.version, "received": payload.version},
        )

    maximum_duration_ms = settings.preview_max_seconds * 1_000
    normalized_duration_ms = (
        normalize_frame_duration_ms(payload.duration_ms)
        if payload.duration_ms is not None
        else None
    )
    proposed_total = sum(
        (
            (
                normalized_duration_ms
                if normalized_duration_ms is not None
                else auto_duration_ms(item)
            )
            if item.id == segment.id
            else effective_duration_ms(item)
        )
        for item in segments
    )
    if proposed_total > maximum_duration_ms:
        raise APIError(
            422,
            "TIMELINE_TOO_LONG",
            f"预览总时长不能超过 {settings.preview_max_seconds} 秒",
            details={
                "duration_ms": proposed_total,
                "maximum_duration_ms": maximum_duration_ms,
            },
        )

    before = {
        "render_duration_ms": segment.render_duration_ms,
        "effective_duration_ms": effective_duration_ms(segment),
        "version": segment.version,
    }
    if segment.render_duration_ms != normalized_duration_ms:
        segment.render_duration_ms = normalized_duration_ms
        segment.version += 1
    session.flush()
    after = {
        "render_duration_ms": segment.render_duration_ms,
        "effective_duration_ms": effective_duration_ms(segment),
        "version": segment.version,
        "requested_duration_ms": payload.duration_ms,
    }
    add_audit(
        session,
        segment.project_id,
        "segment",
        segment.id,
        "segment.timing_updated",
        before=before,
        after=after,
        request_id=request_id,
    )
    session.flush()
    return {
        "segment": _segment_detail(session, segment),
        "timeline": public_preview_plan(session, segment.project_id, maximum_duration_ms),
    }


def reorder_segments(
    session: Session, project_id: str, segment_ids: Sequence[str], request_id: str | None
) -> list[dict]:
    _begin_immediate_if_sqlite(session)
    _get_project(session, project_id)
    statement = select(Segment).where(Segment.project_id == project_id).order_by(Segment.position)
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    segments = session.scalars(statement).all()
    existing_ids = [segment.id for segment in segments]
    if set(existing_ids) != set(segment_ids) or len(existing_ids) != len(segment_ids):
        raise APIError(
            422,
            "INVALID_SEGMENT_ORDER",
            "排序列表必须恰好包含该项目的全部片段",
            details={"expected_count": len(existing_ids), "received_count": len(segment_ids)},
        )
    by_id = {segment.id: segment for segment in segments}
    for index, segment in enumerate(segments):
        segment.position = -(index + 1)
    session.flush()
    for index, segment_id in enumerate(segment_ids):
        by_id[segment_id].position = index
    add_audit(
        session,
        project_id,
        "project",
        project_id,
        "segments.reordered",
        before={"segment_ids": existing_ids},
        after={"segment_ids": list(segment_ids)},
        request_id=request_id,
    )
    session.flush()
    return [_segment_detail(session, by_id[segment_id]) for segment_id in segment_ids]
