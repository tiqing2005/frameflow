from __future__ import annotations

import codecs
import hashlib
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import delete, event, func, select, text as sql_text
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..models import (
    AIRun,
    Asset,
    AuditEvent,
    IdempotencyRecord,
    ImageGeneration,
    Job,
    JobEvent,
    PreviewRender,
    Project,
    Segment,
    Source,
)
from ..serializers import job_dict, project_dict, source_dict
from .common import _get_project, add_audit, dumps, stable_hash, stream_upload_to_path
from .segments import _segment_detail

logger = logging.getLogger(__name__)

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
MAX_SUBTITLE_CUES = 5_000


def _delete_after_rollback(session: Session, path: Path) -> None:
    def cleanup(_session: Session) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Deferred rollback cleanup failed path=%s error_type=%s",
                path,
                type(exc).__name__,
            )

    event.listen(session, "after_rollback", cleanup, once=True)


def _delete_after_commit(session: Session, path: Path) -> None:
    def cleanup(_session: Session) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # The database transaction is already committed. Cleanup is
            # recoverable maintenance and must never turn a successful API
            # mutation into a misleading HTTP 500.
            logger.warning(
                "Deferred commit cleanup failed path=%s error_type=%s",
                path,
                type(exc).__name__,
            )

    event.listen(session, "after_commit", cleanup, once=True)


def _delete_tree_after_commit(session: Session, root: Path, target: Path) -> None:
    """Remove one server-owned staging directory only after DB commit."""

    allowed_root = root.resolve()
    lexical_target = target.absolute()
    try:
        if lexical_target.parent.resolve() != allowed_root:
            return
    except OSError:
        return

    def cleanup(_session: Session) -> None:
        try:
            if lexical_target.is_symlink():
                lexical_target.unlink(missing_ok=True)
                return
            resolved_target = lexical_target.resolve(strict=False)
            if resolved_target == allowed_root or not resolved_target.is_relative_to(
                allowed_root
            ):
                return
            shutil.rmtree(resolved_target, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "Deferred directory cleanup failed path=%s error_type=%s",
                lexical_target,
                type(exc).__name__,
            )

    event.listen(session, "after_commit", cleanup, once=True)


def _valid_source_signature(content: bytes, suffix: str) -> bool:
    head = content[:64]
    if suffix in {".txt", ".srt", ".vtt"}:
        # The bounded signature buffer may end in the middle of a UTF-8 code
        # point. Full incremental UTF-8 validation happens after persistence.
        return b"\x00" not in head
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


def _validate_subtitle_file(path: Path, suffix: str, max_chars: int) -> None:
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    char_count = 0
    cue_count = 0
    pending = ""
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                text = decoder.decode(chunk)
                char_count += len(text)
                if char_count > max_chars:
                    raise APIError(413, "SUBTITLE_TOO_LONG", f"字幕内容最多 {max_chars} 个字符")
                if suffix in {".srt", ".vtt"}:
                    lines = (pending + text).splitlines(keepends=True)
                    pending = ""
                    if lines and not lines[-1].endswith(("\n", "\r")):
                        pending = lines.pop()
                    cue_count += sum("-->" in line for line in lines)
            tail = decoder.decode(b"", final=True)
            char_count += len(tail)
            if char_count > max_chars:
                raise APIError(413, "SUBTITLE_TOO_LONG", f"字幕内容最多 {max_chars} 个字符")
            if suffix in {".srt", ".vtt"}:
                cue_count += int("-->" in (pending + tail))
    except UnicodeDecodeError as exc:
        raise APIError(415, "SOURCE_SIGNATURE_MISMATCH", "字幕文件需使用 UTF-8 编码") from exc
    if cue_count > MAX_SUBTITLE_CUES:
        raise APIError(413, "SUBTITLE_TOO_MANY_CUES", "字幕最多包含 5000 个时间片段")


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


def _lock_idempotent_create(session: Session, key: str | None) -> None:
    """Serialize SQLite's lookup+insert window for a non-empty key."""
    if key and key.strip() and session.get_bind().dialect.name == "sqlite":
        session.execute(sql_text("BEGIN IMMEDIATE"))


def create_text_project(
    session: Session,
    title: str,
    text: str,
    idempotency_key: str | None,
    request_id: str | None,
) -> tuple[Project, Job, bool]:
    title = title.strip()
    text = text.strip()
    if not title or not text:
        raise APIError(422, "VALIDATION_ERROR", "标题和文本内容不能为空")
    if len(title) > 160 or len(text) > 100_000:
        raise APIError(422, "VALIDATION_ERROR", "标题或文本内容超过长度限制")
    content_hash = stable_hash(title, text)
    _lock_idempotent_create(session, idempotency_key)
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


def create_upload_project(
    session: Session,
    settings: Settings,
    title: str,
    filename: str,
    content_type: str | None,
    upload: BinaryIO,
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
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    path = settings.data_dir / "private" / "sources" / stored_name
    size_bytes, sha, head = stream_upload_to_path(upload, path, settings.max_upload_bytes)
    mime_type = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    try:
        if not _valid_source_signature(head, suffix):
            raise APIError(415, "SOURCE_SIGNATURE_MISMATCH", "文件内容与扩展名不匹配或编码不受支持")
        if kind == "text":
            _validate_subtitle_file(path, suffix, settings.max_subtitle_chars)
        request_hash = stable_hash(title, sha)
        _lock_idempotent_create(session, idempotency_key)
        existing = _existing_idempotent(session, idempotency_key, request_hash)
        if existing:
            path.unlink(missing_ok=True)
            return existing[0], existing[1], True
        _delete_after_rollback(session, path)
        project = Project(title=title, status="queued", input_kind=kind)
        session.add(project)
        session.flush()
        source = Source(
            project_id=project.id,
            kind=kind,
            original_filename=safe_name,
            storage_path=str(path),
            public_url=None,
            mime_type=mime_type,
            size_bytes=size_bytes,
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


def delete_project(session: Session, settings: Settings, project_id: str) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite" and not session.in_transaction():
        # Serialize against image/core worker claims so a queued paid task
        # cannot cross its provider boundary while project deletion is fencing it.
        session.execute(sql_text("BEGIN IMMEDIATE"))
    if dialect == "postgresql":
        # Match create/retry/accept/discard and prevent a new generation from
        # appearing after the deletion inventory is locked.
        session.execute(
            sql_text("SELECT pg_advisory_xact_lock(1179795789, 1162431815)")
        )

    image_generation_query = select(ImageGeneration).where(
        ImageGeneration.project_id == project_id
    )
    if dialect == "postgresql":
        # Worker success locks ImageGeneration before it writes project-bound
        # AIRun/Audit rows. Use the same order here to avoid Project→Generation
        # versus Generation→Project deadlocks during concurrent deletion.
        image_generation_query = image_generation_query.with_for_update()
    image_generations = session.scalars(image_generation_query).all()

    project_query = select(Project).where(Project.id == project_id)
    if dialect == "postgresql":
        project_query = project_query.with_for_update()
    project = session.scalar(project_query)
    if project is None:
        raise APIError(404, "PROJECT_NOT_FOUND", "项目不存在")
    source = session.scalar(select(Source).where(Source.project_id == project.id))
    storage_path = source.storage_path if source else None
    preview_paths = [
        Path(value)
        for value in session.scalars(
            select(PreviewRender.storage_path).where(
                PreviewRender.project_id == project.id,
                PreviewRender.storage_path.is_not(None),
            )
        ).all()
        if value
    ]
    image_draft_root = (
        settings.data_dir / "private" / "image-generations"
    ).resolve()
    staging_root = image_draft_root / "staging"
    for generation in image_generations:
        # Deleting the durable row fences queued and in-flight workers. A late
        # provider response cannot publish because its guarded update no longer
        # finds the generation. Accepted global Assets intentionally survive.
        generation.execution_generation += 1
        if generation.output_storage_path:
            draft_path = Path(generation.output_storage_path).resolve(strict=False)
            if draft_path.is_relative_to(image_draft_root):
                _delete_after_commit(session, draft_path)
        _delete_tree_after_commit(
            session,
            staging_root,
            staging_root / generation.id,
        )
        session.delete(generation)
    session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.resource_id == project.id))
    session.delete(project)
    session.flush()
    if storage_path:
        _delete_after_commit(session, Path(storage_path))
    for preview_path in preview_paths:
        _delete_after_commit(session, preview_path)


def dashboard(session: Session) -> dict:
    from ..serializers import run_dict

    project_total = session.scalar(select(func.count()).select_from(Project)) or 0
    asset_total = session.scalar(
        select(func.count()).select_from(Asset).where(Asset.active.is_(True))
    ) or 0
    queued = session.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) or 0
    running = session.scalar(select(func.count()).select_from(Job).where(Job.status == "running")) or 0
    failed = session.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")) or 0
    ready = session.scalar(select(func.count()).select_from(Project).where(Project.status == "ready")) or 0
    recent_projects = session.scalars(select(Project).order_by(Project.updated_at.desc()).limit(6)).all()
    recent_runs = session.scalars(select(AIRun).order_by(AIRun.created_at.desc()).limit(6)).all()
    return {
        "metrics": {
            "projects": project_total,
            "ready_projects": ready,
            "total_assets": asset_total,
            "queued_jobs": queued,
            "running_jobs": running,
            "failed_jobs": failed,
        },
        "recent_projects": [project_dict(project) for project in recent_projects],
        "recent_runs": [run_dict(run) for run in recent_runs],
    }
