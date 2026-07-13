from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..models import Asset, Job, JobEvent, PreviewRender, Segment, Selection
from ..serializers import asset_dict, preview_dict
from .common import _get_project, add_audit, stable_hash


def _duration_ms(segment: Segment) -> int:
    if segment.start_ms is not None and segment.end_ms is not None and segment.end_ms > segment.start_ms:
        return max(1_000, min(12_000, segment.end_ms - segment.start_ms))
    # Text-only projects still get a useful deterministic storyboard duration.
    return max(2_000, min(6_000, int(len(segment.text.strip()) / 10 * 1_000)))


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
        duration = _duration_ms(segment)
        item = {
            "segment_id": segment.id,
            "position": segment.position,
            "text": segment.text,
            "topic": segment.topic,
            "start_ms": cursor,
            "end_ms": cursor + duration,
            "duration_ms": duration,
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
                "asset_updated_at": asset.updated_at.isoformat(),
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


def public_preview_plan(session: Session, project_id: str) -> dict:
    plan = build_preview_plan(session, project_id)
    return {
        **plan,
        "items": [
            {key: value for key, value in item.items() if key != "storage_path"}
            for item in plan["items"]
        ],
    }


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
                "timeline": public_preview_plan(session, project_id),
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
                "timeline": public_preview_plan(session, project_id),
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
        "timeline": public_preview_plan(session, project_id),
        "idempotent_replay": False,
    }


def get_project_preview(session: Session, project_id: str) -> dict:
    _get_project(session, project_id)
    preview = session.scalar(
        select(PreviewRender)
        .where(PreviewRender.project_id == project_id)
        .order_by(PreviewRender.updated_at.desc())
        .limit(1)
    )
    if preview is None:
        return {"preview": None, "timeline": public_preview_plan(session, project_id)}
    job = session.get(Job, preview.job_id) if preview.job_id else None
    return {"preview": preview_dict(preview, job), "timeline": public_preview_plan(session, project_id)}
