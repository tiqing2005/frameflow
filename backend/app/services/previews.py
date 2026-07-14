from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..models import Asset, Job, JobEvent, PreviewRender, Segment, Selection
from ..schemas import TimelineTimingUpdate
from ..serializers import asset_dict, preview_dict
from .common import _get_project, add_audit, stable_hash

SEGMENT_MIN_DURATION_MS = 1_000
SEGMENT_MAX_DURATION_MS = 30_000
FRAME_DURATION_MS = 40
SEGMENT_MIN_DURATION_FRAMES = SEGMENT_MIN_DURATION_MS // FRAME_DURATION_MS
SEGMENT_MAX_DURATION_FRAMES = SEGMENT_MAX_DURATION_MS // FRAME_DURATION_MS


def auto_duration_ms(segment: Segment) -> int:
    if segment.start_ms is not None and segment.end_ms is not None and segment.end_ms > segment.start_ms:
        return max(1_000, min(12_000, segment.end_ms - segment.start_ms))
    # Text-only projects still get a useful deterministic storyboard duration.
    return max(2_000, min(6_000, int(len(segment.text.strip()) / 10 * 1_000)))


def effective_duration_ms(segment: Segment) -> int:
    return segment.render_duration_ms or auto_duration_ms(segment)


def normalize_frame_duration_ms(duration_ms: int) -> int:
    """Round milliseconds to the nearest 25fps frame, with half frames up."""
    return ((int(duration_ms) + FRAME_DURATION_MS // 2) // FRAME_DURATION_MS) * FRAME_DURATION_MS


def allocate_timeline_durations(target_duration_ms: int, weights: list[int]) -> list[int]:
    """Allocate an exact target proportionally with deterministic box constraints."""
    count = len(weights)
    if count == 0:
        raise APIError(409, "PREVIEW_SEGMENTS_EMPTY", "项目没有可调整时长的字幕片段")
    normalized_target_ms = normalize_frame_duration_ms(target_duration_ms)
    target_frames = normalized_target_ms // FRAME_DURATION_MS
    minimum_frames = count * SEGMENT_MIN_DURATION_FRAMES
    maximum_frames = count * SEGMENT_MAX_DURATION_FRAMES
    if target_frames < minimum_frames or target_frames > maximum_frames:
        raise APIError(
            422,
            "TIMELINE_DURATION_INFEASIBLE",
            "目标总时长无法在单片段 1 至 30 秒的范围内完成分配",
            details={
                "target_duration_ms": target_duration_ms,
                "normalized_target_duration_ms": normalized_target_ms,
                "minimum_duration_ms": minimum_frames * FRAME_DURATION_MS,
                "maximum_duration_ms": maximum_frames * FRAME_DURATION_MS,
                "segment_count": count,
            },
        )

    normalized_weights = [max(1, int(weight)) for weight in weights]

    # Find the proportional scale where sum(clamp(scale * weight)) equals the
    # requested total. Bisection only identifies the bounded/free sets; exact
    # Fraction arithmetic below performs the actual millisecond allocation.
    lower_scale = 0.0
    upper_scale = max(
        SEGMENT_MAX_DURATION_FRAMES / weight for weight in normalized_weights
    )
    for _ in range(100):
        scale = (lower_scale + upper_scale) / 2
        projected_total = sum(
            min(
                SEGMENT_MAX_DURATION_FRAMES,
                max(SEGMENT_MIN_DURATION_FRAMES, scale * weight),
            )
            for weight in normalized_weights
        )
        if projected_total < target_frames:
            lower_scale = scale
        else:
            upper_scale = scale
    scale = (lower_scale + upper_scale) / 2

    durations = [0] * count
    active: list[int] = []
    for index, weight in enumerate(normalized_weights):
        projected = scale * weight
        if projected < SEGMENT_MIN_DURATION_FRAMES:
            durations[index] = SEGMENT_MIN_DURATION_FRAMES
        elif projected > SEGMENT_MAX_DURATION_FRAMES:
            durations[index] = SEGMENT_MAX_DURATION_FRAMES
        else:
            active.append(index)

    # Correct the rare floating-point breakpoint ambiguity using exact
    # arithmetic before rounding the active proportional shares.
    while active:
        remaining = target_frames - sum(durations)
        weight_total = sum(normalized_weights[index] for index in active)
        shares = {
            index: Fraction(remaining * normalized_weights[index], weight_total)
            for index in active
        }
        below = [index for index in active if shares[index] < SEGMENT_MIN_DURATION_FRAMES]
        above = [index for index in active if shares[index] > SEGMENT_MAX_DURATION_FRAMES]
        if not below and not above:
            break
        for index in below:
            durations[index] = SEGMENT_MIN_DURATION_FRAMES
        for index in above:
            durations[index] = SEGMENT_MAX_DURATION_FRAMES
        bounded = set(below + above)
        active = [index for index in active if index not in bounded]

    if not active:
        if sum(durations) != target_frames:
            raise RuntimeError("bounded timeline allocation did not reach the exact target")
        return [duration * FRAME_DURATION_MS for duration in durations]

    remaining = target_frames - sum(durations)
    weight_total = sum(normalized_weights[index] for index in active)
    shares = {
        index: Fraction(remaining * normalized_weights[index], weight_total)
        for index in active
    }
    floors = {
        index: shares[index].numerator // shares[index].denominator for index in active
    }
    for index, allocation in floors.items():
        durations[index] = allocation
    leftover = target_frames - sum(durations)
    if leftover:
        # Largest-remainder apportionment keeps the total exact. Position is
        # the stable tie-breaker, so identical requests are reproducible.
        order = sorted(
            active,
            key=lambda index: (-(shares[index] - floors[index]), index),
        )
        for index in order[:leftover]:
            durations[index] += 1

    if sum(durations) != target_frames or any(
        duration < SEGMENT_MIN_DURATION_FRAMES or duration > SEGMENT_MAX_DURATION_FRAMES
        for duration in durations
    ):
        raise RuntimeError("timeline allocation violated its duration invariants")
    return [duration * FRAME_DURATION_MS for duration in durations]


def build_preview_plan(session: Session, project_id: str) -> dict:
    project = _get_project(session, project_id)
    if project.status != "ready":
        raise APIError(409, "PROJECT_NOT_READY", "项目处理完成后才能生成预览视频")
    segments = session.scalars(
        select(Segment).where(Segment.project_id == project.id).order_by(Segment.position)
    ).all()
    if not segments:
        raise APIError(409, "PREVIEW_SEGMENTS_EMPTY", "项目没有可用于预览的字幕片段")

    selections = {
        item.segment_id: item
        for item in session.scalars(
            select(Selection).where(Selection.segment_id.in_([segment.id for segment in segments]))
        ).all()
    }
    asset_ids = {item.asset_id for item in selections.values()}
    assets = {
        item.id: item
        for item in session.scalars(select(Asset).where(Asset.id.in_(asset_ids or {"-"}))).all()
    }

    cursor = 0
    items: list[dict] = []
    fingerprint: list[dict] = []
    for segment in segments:
        selection = selections.get(segment.id)
        asset = assets.get(selection.asset_id) if selection else None
        if asset is None or not asset.active:
            raise APIError(
                409,
                "PREVIEW_SELECTION_MISSING",
                "每个字幕片段都需要选择一个有效素材后才能生成预览",
                details={"segment_id": segment.id},
            )
        path = Path(asset.storage_path or "")
        if not path.is_file():
            raise APIError(
                409,
                "PREVIEW_ASSET_MISSING",
                "预览所需素材文件不存在，请重新上传或更换素材",
                details={"segment_id": segment.id, "asset_id": asset.id},
            )
        automatic_duration = auto_duration_ms(segment)
        duration = segment.render_duration_ms or automatic_duration
        item = {
            "segment_id": segment.id,
            "position": segment.position,
            "text": segment.text,
            "topic": segment.topic,
            "start_ms": cursor,
            "end_ms": cursor + duration,
            "duration_ms": duration,
            "render_duration_ms": segment.render_duration_ms,
            "auto_duration_ms": automatic_duration,
            "effective_duration_ms": duration,
            "duration_source": "manual" if segment.render_duration_ms is not None else "auto",
            "asset": asset_dict(asset),
            "storage_path": str(path),
        }
        items.append(item)
        fingerprint.append(
            {
                "segment_id": segment.id,
                "version": segment.version,
                "position": segment.position,
                "text": segment.text,
                "asset_id": asset.id,
                "duration_ms": duration,
            }
        )
        cursor += duration

    input_hash = stable_hash(
        json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "preview-v1",
    )
    return {
        "project_id": project.id,
        "input_hash": input_hash,
        "segment_count": len(items),
        "duration_ms": cursor,
        "items": items,
    }


def public_preview_plan(
    session: Session,
    project_id: str,
    timeline_max_duration_ms: int = 180_000,
) -> dict:
    return _public_preview_plan(
        build_preview_plan(session, project_id),
        timeline_max_duration_ms,
    )


def _public_preview_plan(plan: dict, timeline_max_duration_ms: int) -> dict:
    return {
        **plan,
        "limits": {
            "segment_min_duration_ms": SEGMENT_MIN_DURATION_MS,
            "segment_max_duration_ms": SEGMENT_MAX_DURATION_MS,
            "timeline_max_duration_ms": timeline_max_duration_ms,
            "frame_duration_ms": FRAME_DURATION_MS,
        },
        "items": [
            {key: value for key, value in item.items() if key != "storage_path"}
            for item in plan["items"]
        ],
    }


def update_timeline_timing(
    session: Session,
    settings: Settings,
    project_id: str,
    payload: TimelineTimingUpdate,
    request_id: str | None,
) -> dict:
    """Atomically fit or restore every clip duration in a project timeline."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    project = _get_project(session, project_id)
    statement = (
        select(Segment)
        .where(Segment.project_id == project.id)
        .order_by(Segment.position, Segment.id)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    segments = session.scalars(statement).all()
    if not segments:
        raise APIError(409, "PREVIEW_SEGMENTS_EMPTY", "项目没有可调整时长的字幕片段")

    maximum_duration_ms = settings.preview_max_seconds * 1_000
    current_plan = build_preview_plan(session, project.id)
    if current_plan["input_hash"] != payload.expected_input_hash:
        raise APIError(
            409,
            "TIMELINE_INPUT_CONFLICT",
            "时间线已被其他操作更新，请刷新后重试",
            details={
                "expected_input_hash": current_plan["input_hash"],
                "received_input_hash": payload.expected_input_hash,
            },
        )

    before = {
        "input_hash": current_plan["input_hash"],
        "duration_ms": current_plan["duration_ms"],
        "render_durations": {
            segment.id: segment.render_duration_ms for segment in segments
        },
    }
    if payload.action == "restore_auto":
        durations: list[int | None] = [None] * len(segments)
        resulting_total = sum(auto_duration_ms(segment) for segment in segments)
    else:
        requested_target_duration_ms = int(payload.target_duration_ms or 0)
        if requested_target_duration_ms > maximum_duration_ms:
            raise APIError(
                422,
                "TIMELINE_TOO_LONG",
                f"预览总时长不能超过 {settings.preview_max_seconds} 秒",
                details={
                    "duration_ms": requested_target_duration_ms,
                    "maximum_duration_ms": maximum_duration_ms,
                },
            )
        if payload.strategy == "current":
            weights = [effective_duration_ms(segment) for segment in segments]
        elif payload.strategy == "equal":
            weights = [1] * len(segments)
        else:
            weights = [len(segment.text.strip()) or 1 for segment in segments]
        durations = allocate_timeline_durations(requested_target_duration_ms, weights)
        resulting_total = sum(durations)

    if resulting_total > maximum_duration_ms:
        raise APIError(
            422,
            "TIMELINE_TOO_LONG",
            f"预览总时长不能超过 {settings.preview_max_seconds} 秒",
            details={
                "duration_ms": resulting_total,
                "maximum_duration_ms": maximum_duration_ms,
            },
        )

    changed_segment_ids: list[str] = []
    for segment, duration in zip(segments, durations, strict=True):
        if segment.render_duration_ms != duration:
            segment.render_duration_ms = duration
            segment.version += 1
            changed_segment_ids.append(segment.id)
    session.flush()
    updated_plan = public_preview_plan(session, project.id, maximum_duration_ms)
    add_audit(
        session,
        project.id,
        "project",
        project.id,
        "timeline.timing_fitted" if payload.action == "fit" else "timeline.timing_restored_auto",
        before=before,
        after={
            "input_hash": updated_plan["input_hash"],
            "duration_ms": updated_plan["duration_ms"],
            "action": payload.action,
            "strategy": payload.strategy if payload.action == "fit" else None,
            "target_duration_ms": payload.target_duration_ms,
            "changed_segment_ids": changed_segment_ids,
        },
        request_id=request_id,
    )
    session.flush()
    return updated_plan


def create_preview_job(
    session: Session,
    settings: Settings,
    project_id: str,
    force: bool,
    request_id: str | None,
) -> dict:
    if settings.database_url.startswith("sqlite"):
        # Serialize the idempotency lookup + insert so concurrent clicks cannot
        # create duplicate preview jobs for the same timeline fingerprint.
        session.execute(text("BEGIN IMMEDIATE"))
    plan = build_preview_plan(session, project_id)
    if plan["duration_ms"] > settings.preview_max_seconds * 1_000:
        raise APIError(
            422,
            "PREVIEW_TOO_LONG",
            f"预览总时长不能超过 {settings.preview_max_seconds} 秒",
            details={"duration_ms": plan["duration_ms"]},
        )

    preview = session.scalar(
        select(PreviewRender).where(
            PreviewRender.project_id == project_id,
            PreviewRender.input_hash == plan["input_hash"],
        )
    )
    previous_job = session.get(Job, preview.job_id) if preview and preview.job_id else None
    if preview:
        # ``force`` means regenerate a completed/failed render. It must not
        # create a second active job for the same fingerprint: doing so would
        # re-point PreviewRender.job_id and leave the first queued job orphaned.
        if previous_job and previous_job.status in {"queued", "running"}:
            return {
                "preview": preview_dict(preview, previous_job),
                "timeline": public_preview_plan(
                    session, project_id, settings.preview_max_seconds * 1_000
                ),
                "idempotent_replay": True,
            }
        if (
            not force
            and preview.status == "succeeded"
            and preview.storage_path
            and Path(preview.storage_path).is_file()
        ):
            return {
                "preview": preview_dict(preview, previous_job),
                "timeline": public_preview_plan(
                    session, project_id, settings.preview_max_seconds * 1_000
                ),
                "idempotent_replay": True,
            }

    job = Job(
        project_id=project_id,
        kind="preview",
        status="queued",
        stage="preview_planning",
        progress=0,
        max_attempts=2,
    )
    session.add(job)
    session.flush()
    if preview is None:
        preview = PreviewRender(
            project_id=project_id,
            job_id=job.id,
            input_hash=plan["input_hash"],
            status="queued",
            duration_ms=plan["duration_ms"],
            segment_count=plan["segment_count"],
        )
        session.add(preview)
    else:
        preview.job_id = job.id
        preview.status = "queued"
        preview.output_url = None
        preview.storage_path = None
        preview.error_message = None
        preview.duration_ms = plan["duration_ms"]
        preview.segment_count = plan["segment_count"]
    session.flush()
    session.add(
        JobEvent(
            job_id=job.id,
            stage="preview_planning",
            progress=0,
            message="预览任务已创建，等待 Worker 生成时间线视频",
        )
    )
    add_audit(
        session,
        project_id,
        "preview",
        preview.id,
        "preview.requested",
        after={
            "job_id": job.id,
            "input_hash": plan["input_hash"],
            "segment_count": plan["segment_count"],
            "duration_ms": plan["duration_ms"],
        },
        request_id=request_id,
    )
    session.flush()
    return {
        "preview": preview_dict(preview, job),
        "timeline": public_preview_plan(
            session, project_id, settings.preview_max_seconds * 1_000
        ),
        "idempotent_replay": False,
    }


def get_project_preview(session: Session, settings: Settings, project_id: str) -> dict:
    _get_project(session, project_id)
    plan = build_preview_plan(session, project_id)
    timeline = _public_preview_plan(plan, settings.preview_max_seconds * 1_000)
    preview = session.scalar(
        select(PreviewRender)
        .where(
            PreviewRender.project_id == project_id,
            PreviewRender.input_hash == plan["input_hash"],
        )
        .order_by(PreviewRender.updated_at.desc())
        .limit(1)
    )
    if preview is None:
        # Keep returning the newest historical render so the client can show
        # it as stale until the current timeline has been rendered.
        preview = session.scalar(
            select(PreviewRender)
            .where(PreviewRender.project_id == project_id)
            .order_by(PreviewRender.updated_at.desc())
            .limit(1)
        )
    if preview is None:
        return {"preview": None, "timeline": timeline}
    job = session.get(Job, preview.job_id) if preview.job_id else None
    return {
        "preview": preview_dict(preview, job),
        "timeline": timeline,
    }
