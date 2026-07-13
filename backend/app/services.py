from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from .asr import REARMABLE_ASR_ERROR_CODES
from .config import Settings
from .errors import APIError
from .models import (
    AIRun,
    Asset,
    AuditEvent,
    FaultControl,
    IdempotencyRecord,
    Job,
    JobEvent,
    Project,
    Recommendation,
    Segment,
    Selection,
    Source,
    utcnow,
)
from .nlp import extract_keywords, infer_topic, rank_assets
from .schemas import AssetPatch, SegmentPatch
from .serializers import (
    asset_dict,
    audit_dict,
    event_dict,
    job_dict,
    project_dict,
    recommendation_dict,
    run_dict,
    segment_base_dict,
    selection_dict,
    source_dict,
)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise APIError(404, "PROJECT_NOT_FOUND", "项目不存在")
    return project


def _get_job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise APIError(404, "JOB_NOT_FOUND", "任务不存在")
    return job


def _get_segment(session: Session, segment_id: str) -> Segment:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")
    return segment


def _get_asset(session: Session, asset_id: str, active_only: bool = False) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None or (active_only and not asset.active):
        raise APIError(404, "ASSET_NOT_FOUND", "素材不存在或已停用")
    return asset


def add_audit(
    session: Session,
    project_id: str | None,
    entity_type: str,
    entity_id: str | None,
    action: str,
    before: Any = None,
    after: Any = None,
    actor: str = "user",
    request_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_json=dumps(before) if before is not None else None,
        after_json=dumps(after) if after is not None else None,
        actor=actor,
        request_id=request_id,
    )
    session.add(event)
    return event


def _existing_idempotent(
    session: Session, key: str | None, request_hash: str
) -> tuple[Project, Job] | None:
    if not key:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > 200:
        raise APIError(400, "IDEMPOTENCY_KEY_TOO_LONG", "Idempotency-Key 最长为 200 个字符")
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == "project:create", IdempotencyRecord.key == key
        )
    )
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise APIError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "相同 Idempotency-Key 已用于不同的请求内容",
            details={"scope": record.scope},
        )
    project = session.get(Project, record.resource_id)
    job = session.get(Job, record.job_id)
    if project is None or job is None:
        session.delete(record)
        session.flush()
        return None
    return project, job


def create_text_project(
    session: Session,
    title: str,
    text: str,
    idempotency_key: str | None,
    request_id: str | None,
) -> tuple[Project, Job, bool]:
    title = title.strip()
    text = text.strip()
    content_hash = stable_hash(title, text)
    existing = _existing_idempotent(session, idempotency_key, content_hash)
    if existing:
        return existing[0], existing[1], True
    project = Project(title=title, status="queued", input_kind="text")
    session.add(project)
    session.flush()
    source = Source(
        project_id=project.id,
        kind="text",
        size_bytes=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        content=text,
    )
    job = Job(project_id=project.id, status="queued", stage="validating", progress=0)
    session.add_all([source, job])
    session.flush()
    session.add(
        JobEvent(job_id=job.id, stage="validating", progress=0, message="任务已持久化，等待 Worker 领取")
    )
    if idempotency_key and idempotency_key.strip():
        session.add(
            IdempotencyRecord(
                scope="project:create",
                key=idempotency_key.strip(),
                request_hash=content_hash,
                resource_id=project.id,
                job_id=job.id,
            )
        )
    add_audit(
        session,
        project.id,
        "project",
        project.id,
        "project.created",
        after={"title": project.title, "input_kind": "text"},
        request_id=request_id,
    )
    session.flush()
    return project, job, False


SOURCE_EXTENSIONS = {
    ".txt": "text",
    ".srt": "text",
    ".vtt": "text",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".mkv": "video",
}


def _valid_source_signature(content: bytes, suffix: str) -> bool:
    head = content[:64]
    if suffix in {".txt", ".srt", ".vtt"}:
        try:
            content[:4096].decode("utf-8-sig")
            return b"\x00" not in head
        except UnicodeDecodeError:
            return False
    if suffix == ".wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if suffix == ".flac":
        return head.startswith(b"fLaC")
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if suffix == ".aac":
        return len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF0 == 0xF0
    if suffix in {".m4a", ".mp4", ".mov"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if suffix in {".webm", ".mkv"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def create_upload_project(
    session: Session,
    settings: Settings,
    title: str,
    filename: str,
    content_type: str | None,
    content: bytes,
    idempotency_key: str | None,
    request_id: str | None,
) -> tuple[Project, Job, bool]:
    title = title.strip()
    if not title or len(title) > 160:
        raise APIError(422, "VALIDATION_ERROR", "标题长度需为 1–160 个字符")
    safe_name = Path(filename or "upload").name[:255]
    suffix = Path(safe_name).suffix.lower()
    kind = SOURCE_EXTENSIONS.get(suffix)
    if kind is None:
        raise APIError(415, "UNSUPPORTED_SOURCE_TYPE", "仅支持 TXT/SRT/VTT、常见音频或视频格式")
    if not content:
        raise APIError(422, "EMPTY_UPLOAD", "上传文件不能为空")
    if len(content) > settings.max_upload_bytes:
        raise APIError(413, "UPLOAD_TOO_LARGE", "上传文件超过大小限制")
    if not _valid_source_signature(content, suffix):
        raise APIError(415, "SOURCE_SIGNATURE_MISMATCH", "文件内容与扩展名不匹配或编码不受支持")
    sha = hashlib.sha256(content).hexdigest()
    request_hash = stable_hash(title, sha)
    existing = _existing_idempotent(session, idempotency_key, request_hash)
    if existing:
        return existing[0], existing[1], True
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    path = settings.data_dir / "media" / "uploads" / "sources" / stored_name
    path.write_bytes(content)
    mime_type = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    try:
        project = Project(title=title, status="queued", input_kind=kind)
        session.add(project)
        session.flush()
        source = Source(
            project_id=project.id,
            kind=kind,
            original_filename=safe_name,
            storage_path=str(path),
            public_url=f"/media/uploads/sources/{stored_name}",
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=sha,
        )
        job = Job(project_id=project.id, status="queued", stage="validating", progress=0)
        session.add_all([source, job])
        session.flush()
        session.add(
            JobEvent(job_id=job.id, stage="validating", progress=0, message="文件已安全保存，等待 Worker 领取")
        )
        if idempotency_key and idempotency_key.strip():
            session.add(
                IdempotencyRecord(
                    scope="project:create",
                    key=idempotency_key.strip(),
                    request_hash=request_hash,
                    resource_id=project.id,
                    job_id=job.id,
                )
            )
        add_audit(
            session,
            project.id,
            "project",
            project.id,
            "project.created",
            after={"title": title, "input_kind": kind, "filename": safe_name, "sha256": sha},
            request_id=request_id,
        )
        session.flush()
        return project, job, False
    except Exception:
        path.unlink(missing_ok=True)
        raise


def list_projects(session: Session, limit: int = 100, offset: int = 0) -> dict:
    total = session.scalar(select(func.count()).select_from(Project)) or 0
    projects = session.scalars(
        select(Project).order_by(Project.updated_at.desc()).offset(offset).limit(limit)
    ).all()
    counts = dict(
        session.execute(
            select(Segment.project_id, func.count(Segment.id))
            .where(Segment.project_id.in_([item.id for item in projects] or ["-"]))
            .group_by(Segment.project_id)
        ).all()
    )
    return {"items": [project_dict(item, counts.get(item.id, 0)) for item in projects], "total": total}


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


def project_detail(session: Session, project_id: str) -> dict:
    project = _get_project(session, project_id)
    source = session.scalar(select(Source).where(Source.project_id == project.id))
    current_job = session.scalar(
        select(Job).where(Job.project_id == project.id).order_by(Job.created_at.desc()).limit(1)
    )
    segments = session.scalars(
        select(Segment).where(Segment.project_id == project.id).order_by(Segment.position)
    ).all()
    ai_runs = session.scalar(select(func.count()).select_from(AIRun).where(AIRun.project_id == project.id)) or 0
    audit_events = session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.project_id == project.id)
    ) or 0
    degraded = bool(
        session.scalar(
            select(func.count()).select_from(AIRun).where(
                AIRun.project_id == project.id, AIRun.degraded.is_(True)
            )
        )
    )
    return {
        "project": project_dict(project, len(segments)),
        "current_job": job_dict(current_job) if current_job else None,
        "source": source_dict(source),
        "segments": [_segment_detail(session, segment) for segment in segments],
        "trace_summary": {
            "degraded": degraded,
            "ai_runs": ai_runs,
            "audit_events": audit_events,
            "strategy": "0.55×字符n-gram TF-IDF + 0.30×关键词 + 0.15×标签/主题",
        },
    }


def delete_project(session: Session, project_id: str) -> None:
    project = _get_project(session, project_id)
    source = session.scalar(select(Source).where(Source.project_id == project.id))
    storage_path = source.storage_path if source else None
    session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.resource_id == project.id))
    session.delete(project)
    session.flush()
    if storage_path:
        Path(storage_path).unlink(missing_ok=True)


def get_job_detail(session: Session, job_id: str) -> dict:
    job = _get_job(session, job_id)
    events = session.scalars(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.created_at, JobEvent.id)
    ).all()
    return {"job": job_dict(job), "events": [event_dict(event) for event in events]}


def retry_job(session: Session, job_id: str, request_id: str | None) -> dict:
    job = _get_job(session, job_id)
    rearmable = job.error_code in REARMABLE_ASR_ERROR_CODES
    if job.status != "failed":
        raise APIError(409, "INVALID_STATE", "只有失败且明确可重试的任务才能重试")
    if not job.retryable and not rearmable:
        raise APIError(409, "JOB_NOT_RETRYABLE", "该任务失败原因不可重试")
    next_max_attempts = job.max_attempts
    if job.attempt >= job.max_attempts:
        if not rearmable:
            raise APIError(409, "JOB_ATTEMPTS_EXHAUSTED", "任务已达到最大尝试次数")
        next_max_attempts = job.attempt + 1
    before = {
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retryable": job.retryable,
        "attempt": job.attempt,
    }
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "failed", Job.attempt == job.attempt)
        .values(
            status="queued",
            stage="validating",
            retryable=True if rearmable else job.retryable,
            max_attempts=next_max_attempts,
            finished_at=None,
            next_run_at=utcnow(),
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if result.rowcount != 1:
        session.expire_all()
        raise APIError(409, "JOB_RETRY_CONFLICT", "任务状态已变化，请刷新后再试")
    session.expire(job)
    job = session.get(Job, job.id)
    project = _get_project(session, job.project_id)
    project.status = "queued"
    session.add(
        JobEvent(
            job_id=job.id,
            stage="validating",
            progress=job.progress,
            message=(
                f"已请求重新执行原任务；保留第 {job.attempt} 次失败"
                f"（{job.error_code or 'UNKNOWN'}：{job.error_message or '未提供错误原因'}）"
            ),
        )
    )
    add_audit(
        session,
        project.id,
        "job",
        job.id,
        "job.retried",
        before=before,
        after={
            "status": "queued",
            "next_attempt": job.attempt + 1,
            "project_reused": True,
            "source_reused": True,
        },
        request_id=request_id,
    )
    session.flush()
    return get_job_detail(session, job.id)


def cancel_job(session: Session, job_id: str, request_id: str | None) -> dict:
    job = _get_job(session, job_id)
    if job.status == "canceled":
        return get_job_detail(session, job.id)
    if job.status in {"succeeded", "failed"}:
        raise APIError(409, "JOB_NOT_CANCELABLE", "只有排队中或运行中的任务可以取消")
    job.status = "canceled"
    job.retryable = False
    job.finished_at = utcnow()
    job.lease_owner = None
    job.lease_expires_at = None
    project = _get_project(session, job.project_id)
    project.status = "canceled"
    session.add(
        JobEvent(job_id=job.id, stage=job.stage, progress=job.progress, message="任务已由用户取消", level="warning")
    )
    add_audit(
        session,
        project.id,
        "job",
        job.id,
        "job.canceled",
        after={"stage": job.stage, "progress": job.progress},
        request_id=request_id,
    )
    session.flush()
    return get_job_detail(session, job.id)


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


def rematch_segment(
    session: Session,
    segment: Segment,
    request_id: str | None,
    actor: str = "user",
    degraded: bool = False,
) -> dict:
    keywords = json.loads(segment.keywords_json)
    assets = _asset_rank_payloads(session)
    ranked = rank_assets(segment.text, segment.topic, keywords, assets, minimum=3)
    if len(ranked) < 3:
        raise APIError(409, "INSUFFICIENT_ASSETS", "至少需要 3 个启用素材才能重新匹配")
    input_hash = stable_hash(segment.text, segment.topic, dumps(keywords), "hybrid-v1")
    run = AIRun(
        project_id=segment.project_id,
        segment_id=segment.id,
        operation="segment_rematch",
        provider="rules",
        model="char-ngram-tfidf-hybrid-v1",
        prompt_version="rules-v1",
        input_hash=input_hash,
        status="succeeded",
        degraded=degraded,
        output_summary_json=dumps({"candidate_count": len(ranked), "top_score": ranked[0].total_score}),
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


def patch_segment(session: Session, segment_id: str, payload: SegmentPatch, request_id: str | None) -> dict:
    segment = _get_segment(session, segment_id)
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
    return rematch_segment(session, segment, request_id, actor="system")


def reorder_segments(
    session: Session, project_id: str, segment_ids: Sequence[str], request_id: str | None
) -> list[dict]:
    _get_project(session, project_id)
    segments = session.scalars(
        select(Segment).where(Segment.project_id == project_id).order_by(Segment.position)
    ).all()
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


def list_assets(
    session: Session, q: str | None, kind: str | None, tag: str | None, include_inactive: bool = False
) -> dict:
    statement = select(Asset)
    count_statement = select(func.count()).select_from(Asset)
    filters = []
    if not include_inactive:
        filters.append(Asset.active.is_(True))
    if q and q.strip():
        query = f"%{q.strip().lower()}%"
        filters.append(
            or_(
                func.lower(Asset.name).like(query),
                func.lower(Asset.tags_json).like(query),
                func.lower(Asset.keywords_json).like(query),
            )
        )
    if kind:
        filters.append(Asset.kind == kind)
    if tag and tag.strip():
        filters.append(func.lower(Asset.tags_json).like(f"%{tag.strip().lower()}%"))
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)
    items = session.scalars(statement.order_by(Asset.is_seed.desc(), Asset.created_at.desc())).all()
    total = session.scalar(count_statement) or 0
    return {"items": [asset_dict(asset) for asset in items], "total": total}


ASSET_EXTENSIONS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
}


def _valid_asset_signature(content: bytes, suffix: str) -> bool:
    head = content[:64]
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if suffix in {".mp4", ".mov"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if suffix == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _parse_csv(value: str) -> list[str]:
    values = re.split(r"[,，;；\n]", value or "")
    return list(dict.fromkeys(item.strip()[:60] for item in values if item.strip()))[:20]


def create_asset(
    session: Session,
    settings: Settings,
    filename: str,
    content_type: str | None,
    content: bytes,
    name: str,
    tags: str,
    keywords: str,
    request_id: str | None,
) -> Asset:
    name = name.strip()
    if not name or len(name) > 160:
        raise APIError(422, "VALIDATION_ERROR", "素材名称长度需为 1–160 个字符")
    safe_name = Path(filename or "asset").name[:255]
    suffix = Path(safe_name).suffix.lower()
    kind = ASSET_EXTENSIONS.get(suffix)
    if kind is None:
        raise APIError(415, "UNSUPPORTED_ASSET_TYPE", "仅支持常见图片或 MP4/WebM/MOV 视频素材")
    if not content:
        raise APIError(422, "EMPTY_UPLOAD", "上传文件不能为空")
    if len(content) > settings.max_upload_bytes:
        raise APIError(413, "UPLOAD_TOO_LARGE", "上传文件超过大小限制")
    if not _valid_asset_signature(content, suffix):
        raise APIError(415, "ASSET_SIGNATURE_MISMATCH", "素材内容与扩展名不匹配；用户上传 SVG 已禁用以避免脚本风险")
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    path = settings.data_dir / "media" / "uploads" / "assets" / stored_name
    path.write_bytes(content)
    try:
        tag_values = _parse_csv(tags)
        keyword_values = _parse_csv(keywords)
        if not keyword_values:
            keyword_values = extract_keywords(name + " " + " ".join(tag_values))
        asset = Asset(
            name=name,
            kind=kind,
            public_url=f"/media/uploads/assets/{stored_name}",
            storage_path=str(path),
            mime_type=content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
            size_bytes=len(content),
            tags_json=dumps(tag_values),
            keywords_json=dumps(keyword_values),
            is_seed=False,
            active=True,
        )
        session.add(asset)
        session.flush()
        add_audit(
            session,
            None,
            "asset",
            asset.id,
            "asset.created",
            after=asset_dict(asset),
            request_id=request_id,
        )
        return asset
    except Exception:
        path.unlink(missing_ok=True)
        raise


def patch_asset(
    session: Session, asset_id: str, payload: AssetPatch, request_id: str | None
) -> Asset:
    asset = _get_asset(session, asset_id)
    before = asset_dict(asset)
    if payload.name is not None:
        asset.name = payload.name
    if payload.tags is not None:
        asset.tags_json = dumps(payload.tags)
    if payload.keywords is not None:
        asset.keywords_json = dumps(payload.keywords)
    if payload.active is not None:
        if asset.is_seed and not payload.active:
            active_count = session.scalar(
                select(func.count()).select_from(Asset).where(Asset.active.is_(True))
            ) or 0
            if active_count <= 3:
                raise APIError(409, "MINIMUM_ASSET_GUARD", "至少保留 3 个启用素材")
        asset.active = payload.active
    session.flush()
    add_audit(
        session,
        None,
        "asset",
        asset.id,
        "asset.updated",
        before=before,
        after=asset_dict(asset),
        request_id=request_id,
    )
    return asset


def list_runs(session: Session, limit: int = 100, offset: int = 0) -> dict:
    total = session.scalar(select(func.count()).select_from(AIRun)) or 0
    runs = session.scalars(
        select(AIRun).order_by(AIRun.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {"items": [run_dict(run) for run in runs], "total": total}


def list_audit(
    session: Session, project_id: str | None, limit: int = 200, offset: int = 0
) -> dict:
    statement = select(AuditEvent)
    count_statement = select(func.count()).select_from(AuditEvent)
    if project_id:
        statement = statement.where(AuditEvent.project_id == project_id)
        count_statement = count_statement.where(AuditEvent.project_id == project_id)
    total = session.scalar(count_statement) or 0
    events = session.scalars(
        statement.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {"items": [audit_dict(event) for event in events], "total": total}


def set_fault(session: Session, mode: str, request_id: str | None) -> dict:
    control = session.get(FaultControl, 1)
    if control is None:
        control = FaultControl(id=1, next_mode=mode)
        session.add(control)
    else:
        control.next_mode = mode
        control.updated_at = utcnow()
    add_audit(
        session,
        None,
        "demo_fault",
        "1",
        "demo.fault_configured",
        after={"mode": mode},
        request_id=request_id,
    )
    session.flush()
    return {
        "mode": mode,
        "message": {
            "none": "已清除下一次故障",
            "ai_degrade": "下一次任务将模拟 AI 不可用并自动规则降级",
            "job_fail": "下一次任务将模拟可重试的处理失败",
        }[mode],
    }


def dashboard(session: Session) -> dict:
    project_total = session.scalar(select(func.count()).select_from(Project)) or 0
    asset_total = session.scalar(
        select(func.count()).select_from(Asset).where(Asset.active.is_(True))
    ) or 0
    running = session.scalar(
        select(func.count()).select_from(Job).where(Job.status.in_(["queued", "running"]))
    ) or 0
    failed = session.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")) or 0
    ready = session.scalar(select(func.count()).select_from(Project).where(Project.status == "ready")) or 0
    recent_projects = session.scalars(select(Project).order_by(Project.updated_at.desc()).limit(6)).all()
    recent_runs = session.scalars(select(AIRun).order_by(AIRun.created_at.desc()).limit(6)).all()
    return {
        "metrics": {
            "projects": project_total,
            "ready_projects": ready,
            "total_assets": asset_total,
            "running_jobs": running,
            "failed_jobs": failed,
        },
        "recent_projects": [project_dict(project) for project in recent_projects],
        "recent_runs": [run_dict(run) for run in recent_runs],
    }
