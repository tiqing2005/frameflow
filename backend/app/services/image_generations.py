from __future__ import annotations

import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..image_generation import IMAGE_GENERATION_HARD_MAX_ATTEMPTS, PROVIDER_NAME
from ..models import Asset, ImageGeneration, Segment, Selection, utcnow
from ..schemas import (
    ImageGenerationAccept,
    ImageGenerationCreate,
    SegmentImageGenerationCreate,
)
from ..serializers import (
    asset_dict,
    image_generation_dict,
    segment_base_dict,
    selection_dict,
)
from .assets import create_asset
from .common import add_audit, stable_hash
from .projects import _delete_after_commit


logger = logging.getLogger(__name__)


def _staging_directory(settings: Settings, generation_id: str) -> Path:
    return settings.data_dir / "private" / "image-generations" / "staging" / generation_id


def _delete_staging_after_commit(
    session: Session, settings: Settings, generation_id: str
) -> None:
    root = (settings.data_dir / "private" / "image-generations" / "staging").resolve()
    candidate = _staging_directory(settings, generation_id)
    target = candidate.resolve(strict=False)
    if not target.is_relative_to(root) or target == root:
        return

    def cleanup(_session: Session) -> None:
        try:
            if candidate.is_symlink():
                candidate.unlink(missing_ok=True)
            else:
                shutil.rmtree(candidate, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "Deferred image staging cleanup failed generation_id=%s error_type=%s",
                generation_id,
                type(exc).__name__,
            )

    event.listen(session, "after_commit", cleanup, once=True)


def _lock_writes(session: Session) -> str:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite" and not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))
    elif dialect == "postgresql":
        # Serialize quota checks and inserts across all API processes. The
        # transaction-scoped lock is released automatically on commit/rollback.
        session.execute(text("SELECT pg_advisory_xact_lock(1179795789, 1162431815)"))
    return dialect


def _generation_query(generation_id: str, dialect: str):
    query = select(ImageGeneration).where(ImageGeneration.id == generation_id)
    if dialect == "postgresql":
        query = query.with_for_update()
    return query


def get_image_generation(session: Session, generation_id: str) -> ImageGeneration:
    generation = session.get(ImageGeneration, generation_id)
    if generation is None:
        raise APIError(404, "IMAGE_GENERATION_NOT_FOUND", "文生图任务不存在")
    return generation


def _normalize_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.replace("\x00", " ").split()).strip()
    if not normalized:
        raise APIError(422, "IMAGE_PROMPT_EMPTY", "请输入有效的图片描述")
    return normalized[:2_000]


def _effective_prompt(prompt: str, aspect_ratio: str) -> str:
    ratio_label = {
        "16:9": "横向 16:9 视频配图",
        "1:1": "方形 1:1 素材",
        "9:16": "竖向 9:16 素材",
    }[aspect_ratio]
    return (
        f"{prompt}\n\n创作要求：生成高质量的{ratio_label}；主体清晰，构图完整，"
        "画面中不要出现字幕、品牌标志、水印、二维码或无关文字。"
    )[:2_400]


def _segment_prompt(segment: Segment) -> str:
    try:
        keywords = [str(item) for item in json.loads(segment.keywords_json) if str(item).strip()]
    except (TypeError, json.JSONDecodeError):
        keywords = []
    keyword_text = "、".join(keywords[:6])
    return _normalize_prompt(
        "请为下面这段视频字幕创作一张语义准确、可直接用于剪辑的配图。"
        f"字幕：{segment.text[:1_200]}。主题：{segment.topic}。"
        + (f"关键词：{keyword_text}。" if keyword_text else "")
    )


def _default_name(prompt: str, segment: Segment | None) -> str:
    if segment is not None and segment.topic.strip():
        return f"AI 生成 · {segment.topic.strip()}"[:160]
    compact = prompt.strip().rstrip("。！？!?；;")
    return (f"AI 生成 · {compact[:48]}" if compact else "AI 生成素材")[:160]


def _creation_intent_hash(
    *,
    prompt: str | None,
    name: str | None,
    aspect_ratio: str,
    segment_id: str | None,
    auto_import: bool,
    auto_select: bool,
) -> str:
    """Hash only immutable HTTP intent, never mutable segment state."""

    return stable_hash(
        prompt or "",
        name or "",
        aspect_ratio,
        segment_id or "",
        str(auto_import),
        str(auto_select),
        "image-generation-create-intent-v2",
    )


def create_image_generation(
    session: Session,
    settings: Settings,
    payload: ImageGenerationCreate | SegmentImageGenerationCreate,
    idempotency_key: str | None,
    request_id: str | None,
    *,
    segment_id: str | None = None,
    prompt_override: str | None = None,
) -> tuple[ImageGeneration, bool]:
    if not settings.image_api_base_url or not settings.image_api_key or not settings.image_model:
        raise APIError(
            503,
            "IMAGE_GENERATION_NOT_CONFIGURED",
            "文生图服务尚未配置",
            retryable=False,
        )
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 200:
        raise APIError(400, "IDEMPOTENCY_KEY_TOO_LONG", "Idempotency-Key 最长为 200 个字符")

    resolved_segment_id = segment_id or getattr(payload, "segment_id", None)
    raw_prompt = prompt_override if prompt_override is not None else payload.prompt
    request_hash = _creation_intent_hash(
        prompt=raw_prompt,
        name=payload.name,
        aspect_ratio=payload.aspect_ratio,
        segment_id=resolved_segment_id,
        auto_import=payload.auto_import,
        auto_select=payload.auto_select,
    )

    dialect = _lock_writes(session)
    if key:
        existing = session.scalar(
            select(ImageGeneration).where(ImageGeneration.idempotency_key == key)
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise APIError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "相同 Idempotency-Key 已用于不同的文生图请求",
                )
            return existing, True

    segment: Segment | None = None
    if resolved_segment_id:
        segment_query = select(Segment).where(Segment.id == resolved_segment_id)
        if dialect == "postgresql":
            segment_query = segment_query.with_for_update()
        segment = session.scalar(segment_query)
        if segment is None:
            raise APIError(404, "SEGMENT_NOT_FOUND", "字幕片段不存在")

    prompt = _normalize_prompt(raw_prompt or _segment_prompt(segment))
    name = (payload.name or _default_name(prompt, segment)).strip()[:160]

    pending = session.scalar(
        select(func.count())
        .select_from(ImageGeneration)
        .where(ImageGeneration.status.in_(("queued", "running")))
    ) or 0
    if pending >= settings.image_max_pending:
        raise APIError(
            429,
            "IMAGE_QUEUE_FULL",
            "文生图排队任务已达上限，请等待现有任务完成",
            retryable=True,
            details={"maximum_pending": settings.image_max_pending},
        )
    if settings.image_daily_limit > 0:
        cutoff = utcnow() - timedelta(hours=24)
        recent = session.scalar(
            select(func.count())
            .select_from(ImageGeneration)
            .where(ImageGeneration.created_at >= cutoff)
        ) or 0
        if recent >= settings.image_daily_limit:
            raise APIError(
                429,
                "IMAGE_DAILY_LIMIT_REACHED",
                "过去 24 小时的文生图次数已达上限",
                retryable=True,
                details={"daily_limit": settings.image_daily_limit},
            )

    now = utcnow()
    generation = ImageGeneration(
        project_id=segment.project_id if segment else None,
        segment_id=segment.id if segment else None,
        segment_version=segment.version if segment else None,
        source="segment_shortfall" if segment else "library",
        prompt=prompt,
        effective_prompt=_effective_prompt(prompt, payload.aspect_ratio),
        name=name,
        aspect_ratio=payload.aspect_ratio,
        provider=PROVIDER_NAME,
        model=settings.image_model,
        status="queued",
        max_attempts=2,
        next_run_at=now,
        retryable=True,
        auto_import=payload.auto_import,
        auto_select=payload.auto_select,
        idempotency_key=key,
        request_hash=request_hash,
        expires_at=now + timedelta(hours=settings.image_draft_retention_hours),
    )
    try:
        with session.begin_nested():
            session.add(generation)
            session.flush()
    except IntegrityError:
        if not key:
            raise
        existing = session.scalar(
            select(ImageGeneration).where(ImageGeneration.idempotency_key == key)
        )
        if existing is None or existing.request_hash != request_hash:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "文生图请求幂等冲突")
        return existing, True
    add_audit(
        session,
        generation.project_id,
        "image_generation",
        generation.id,
        "image_generation.requested",
        after={
            "segment_id": generation.segment_id,
            "source": generation.source,
            "aspect_ratio": generation.aspect_ratio,
            "provider": generation.provider,
            "model": generation.model,
            "prompt_hash": stable_hash(generation.prompt),
            "auto_import": generation.auto_import,
            "auto_select": generation.auto_select,
        },
        request_id=request_id,
    )
    session.flush()
    return generation, False


def list_image_generations(
    session: Session,
    *,
    status: str | None = None,
    segment_id: str | None = None,
    include_discarded: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    filters = []
    if status:
        filters.append(ImageGeneration.status == status)
    if segment_id:
        filters.append(ImageGeneration.segment_id == segment_id)
    if not include_discarded:
        filters.append(ImageGeneration.discarded_at.is_(None))
    statement = select(ImageGeneration)
    count_statement = select(func.count()).select_from(ImageGeneration)
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)
    items = session.scalars(
        statement.order_by(ImageGeneration.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "items": [image_generation_dict(item) for item in items],
        "total": session.scalar(count_statement) or 0,
    }


def image_generation_detail(session: Session, generation_id: str) -> dict:
    generation = get_image_generation(session, generation_id)
    asset = session.get(Asset, generation.asset_id) if generation.asset_id else None
    segment = session.get(Segment, generation.segment_id) if generation.segment_id else None
    selection = (
        session.scalar(select(Selection).where(Selection.segment_id == segment.id))
        if segment
        else None
    )
    selected_asset = session.get(Asset, selection.asset_id) if selection else None
    return {
        "generation": image_generation_dict(generation),
        "asset": asset_dict(asset) if asset else None,
        "selection": (
            selection_dict(selection, selected_asset)
            if selection and selected_asset
            else None
        ),
        "segment": segment_base_dict(segment) if segment else None,
    }


def image_generation_content_path(
    session: Session, settings: Settings, generation_id: str
) -> Path:
    generation = get_image_generation(session, generation_id)
    if generation.discarded_at is not None:
        raise APIError(410, "IMAGE_DRAFT_DISCARDED", "该生成草稿已被丢弃")
    raw_path = generation.output_storage_path
    allowed_root = (settings.data_dir / "private" / "image-generations").resolve()
    if not raw_path and generation.asset_id:
        asset = session.get(Asset, generation.asset_id)
        raw_path = asset.storage_path if asset else None
        allowed_root = (settings.data_dir / "media" / "uploads" / "assets").resolve()
    if generation.status != "succeeded" or not raw_path:
        raise APIError(409, "IMAGE_CONTENT_NOT_READY", "生成图片尚未就绪")
    path = Path(raw_path).resolve(strict=False)
    if not path.is_relative_to(allowed_root) or not path.is_file():
        raise APIError(404, "IMAGE_CONTENT_MISSING", "生成图片文件不存在")
    return path


def retry_image_generation(
    session: Session, generation_id: str, request_id: str | None
) -> ImageGeneration:
    dialect = _lock_writes(session)
    generation = session.scalar(_generation_query(generation_id, dialect))
    if generation is None:
        raise APIError(404, "IMAGE_GENERATION_NOT_FOUND", "文生图任务不存在")
    if generation.discarded_at is not None:
        raise APIError(409, "IMAGE_DRAFT_DISCARDED", "已丢弃的生成任务不能重试")
    if generation.status != "failed":
        raise APIError(409, "IMAGE_RETRY_INVALID_STATE", "只有失败的文生图任务可以重试")
    if generation.attempt >= IMAGE_GENERATION_HARD_MAX_ATTEMPTS:
        raise APIError(
            409,
            "IMAGE_RETRY_LIMIT_REACHED",
            f"该文生图任务已达到 {IMAGE_GENERATION_HARD_MAX_ATTEMPTS} 次服务调用上限",
        )
    if not generation.retryable:
        raise APIError(409, "IMAGE_NOT_RETRYABLE", "该失败原因不适合重试")
    before = image_generation_dict(generation)
    generation.status = "queued"
    generation.max_attempts = min(
        IMAGE_GENERATION_HARD_MAX_ATTEMPTS,
        max(generation.max_attempts, generation.attempt + 1),
    )
    generation.next_run_at = utcnow()
    generation.lease_owner = None
    generation.lease_expires_at = None
    # This durable marker distinguishes an explicit user-authorized provider
    # retry from automatic recovery of an ambiguous stale submission.
    generation.error_code = "IMAGE_MANUAL_RETRY_AUTHORIZED"
    generation.error_message = None
    generation.finished_at = None
    add_audit(
        session,
        generation.project_id,
        "image_generation",
        generation.id,
        "image_generation.retried",
        before=before,
        after={"status": "queued", "next_attempt": generation.attempt + 1},
        request_id=request_id,
    )
    session.flush()
    return generation


def cancel_image_generation(
    session: Session,
    settings: Settings,
    generation_id: str,
    request_id: str | None,
) -> ImageGeneration:
    dialect = _lock_writes(session)
    generation = session.scalar(_generation_query(generation_id, dialect))
    if generation is None:
        raise APIError(404, "IMAGE_GENERATION_NOT_FOUND", "文生图任务不存在")
    if generation.status == "canceled":
        return generation
    if generation.status not in {"queued", "running"}:
        raise APIError(409, "IMAGE_CANCEL_INVALID_STATE", "只有排队中或生成中的任务可以取消")
    generation.status = "canceled"
    generation.execution_generation += 1
    generation.retryable = False
    generation.finished_at = utcnow()
    generation.lease_owner = None
    generation.lease_expires_at = None
    _delete_staging_after_commit(session, settings, generation.id)
    add_audit(
        session,
        generation.project_id,
        "image_generation",
        generation.id,
        "image_generation.canceled",
        after={"attempt": generation.attempt},
        request_id=request_id,
    )
    session.flush()
    return generation


def _selection_for_segment(
    session: Session, segment: Segment | None
) -> tuple[Selection | None, Asset | None]:
    if segment is None:
        return None, None
    selection = session.scalar(select(Selection).where(Selection.segment_id == segment.id))
    asset = session.get(Asset, selection.asset_id) if selection else None
    return selection, asset


def _validate_generated_selection(
    generation: ImageGeneration,
    segment: Segment | None,
    payload: ImageGenerationAccept,
) -> None:
    if not payload.select_for_segment:
        return
    if segment is None:
        raise APIError(409, "IMAGE_SEGMENT_MISSING", "关联字幕片段已不存在")
    expected = payload.expected_segment_version or generation.segment_version
    if expected is not None and segment.version != expected:
        raise APIError(
            409,
            "IMAGE_SEGMENT_VERSION_CONFLICT",
            "字幕片段已更新，请确认新内容后再选择生成图片",
            details={"expected": expected, "current": segment.version},
        )


def _upsert_generated_selection(
    session: Session,
    segment: Segment,
    asset: Asset,
    request_id: str | None,
    actor: str,
) -> Selection:
    selection = session.scalar(
        select(Selection).where(Selection.segment_id == segment.id)
    )
    if (
        selection is not None
        and selection.asset_id == asset.id
        and selection.source == "generated"
    ):
        return selection
    before = (
        {"asset_id": selection.asset_id, "source": selection.source}
        if selection
        else None
    )
    if selection is None:
        selection = Selection(
            segment_id=segment.id, asset_id=asset.id, source="generated"
        )
        session.add(selection)
    else:
        selection.asset_id = asset.id
        selection.source = "generated"
        selection.updated_at = utcnow()
    session.flush()
    add_audit(
        session,
        segment.project_id,
        "selection",
        selection.id,
        "selection.changed",
        before=before,
        after={"asset_id": asset.id, "source": "generated"},
        actor=actor,
        request_id=request_id,
    )
    return selection


def accept_image_generation(
    session: Session,
    settings: Settings,
    generation_id: str,
    payload: ImageGenerationAccept,
    request_id: str | None,
    *,
    actor: str = "user",
) -> dict:
    dialect = _lock_writes(session)
    generation = session.scalar(_generation_query(generation_id, dialect))
    if generation is None:
        raise APIError(404, "IMAGE_GENERATION_NOT_FOUND", "文生图任务不存在")
    segment_query = (
        select(Segment).where(Segment.id == generation.segment_id)
        if generation.segment_id
        else None
    )
    if segment_query is not None and dialect == "postgresql":
        segment_query = segment_query.with_for_update()
    segment = session.scalar(segment_query) if segment_query is not None else None

    if generation.asset_id:
        asset_query = select(Asset).where(Asset.id == generation.asset_id)
        if dialect == "postgresql":
            asset_query = asset_query.with_for_update()
        asset = session.scalar(asset_query)
        if asset is None:
            generation.asset_id = None
        else:
            _validate_generated_selection(generation, segment, payload)
            if payload.select_for_segment and segment is not None:
                if not asset.active:
                    raise APIError(
                        409,
                        "ASSET_INACTIVE",
                        "已入库的生成素材已停用，不能再选择到字幕片段。",
                    )
                selection = _upsert_generated_selection(
                    session, segment, asset, request_id, actor
                )
                selected_asset = asset
            else:
                selection, selected_asset = _selection_for_segment(session, segment)
            return {
                "generation": image_generation_dict(generation),
                "asset": asset_dict(asset),
                "selection": (
                    selection_dict(selection, selected_asset)
                    if selection and selected_asset
                    else None
                ),
                "segment": segment_base_dict(segment) if segment else None,
                "idempotent_replay": True,
            }
    if generation.status != "succeeded":
        raise APIError(409, "IMAGE_ACCEPT_NOT_READY", "只有生成成功的图片可以加入素材库")
    if generation.discarded_at is not None:
        raise APIError(409, "IMAGE_DRAFT_DISCARDED", "已丢弃的图片不能加入素材库")
    if not generation.output_storage_path:
        raise APIError(404, "IMAGE_CONTENT_MISSING", "生成图片文件不存在")
    draft_path = Path(generation.output_storage_path).resolve(strict=False)
    draft_root = (settings.data_dir / "private" / "image-generations").resolve()
    if not draft_path.is_relative_to(draft_root) or not draft_path.is_file():
        raise APIError(404, "IMAGE_CONTENT_MISSING", "生成图片文件不存在")

    should_select = payload.select_for_segment
    _validate_generated_selection(generation, segment, payload)

    with draft_path.open("rb") as source:
        asset = create_asset(
            session,
            settings,
            "generated.png",
            "image/png",
            source,
            (payload.name or generation.name).strip()[:160],
            "",
            "",
            request_id,
        )
    generation.asset_id = asset.id
    generation.accepted_at = utcnow()
    generation.output_storage_path = None
    generation.lease_owner = None
    generation.lease_expires_at = None
    generation.error_code = None
    generation.error_message = None
    _delete_after_commit(session, draft_path)
    _delete_staging_after_commit(session, settings, generation.id)

    selection: Selection | None = None
    selected_asset: Asset | None = None
    if should_select and segment is not None:
        selection = _upsert_generated_selection(
            session, segment, asset, request_id, actor
        )
        selected_asset = asset
    add_audit(
        session,
        generation.project_id,
        "image_generation",
        generation.id,
        "image_generation.accepted",
        after={
            "asset_id": asset.id,
            "segment_id": segment.id if segment else None,
            "selected": bool(selection),
            "tagging_status": asset.tagging_status,
        },
        actor=actor,
        request_id=request_id,
    )
    session.flush()
    return {
        "generation": image_generation_dict(generation),
        "asset": asset_dict(asset),
        "selection": (
            selection_dict(selection, selected_asset)
            if selection and selected_asset
            else None
        ),
        "segment": segment_base_dict(segment) if segment else None,
        "idempotent_replay": False,
    }


def discard_image_generation(
    session: Session, settings: Settings, generation_id: str, request_id: str | None
) -> None:
    dialect = _lock_writes(session)
    generation = session.scalar(_generation_query(generation_id, dialect))
    if generation is None:
        raise APIError(404, "IMAGE_GENERATION_NOT_FOUND", "文生图任务不存在")
    if generation.asset_id:
        raise APIError(
            409,
            "IMAGE_ALREADY_ACCEPTED",
            "图片已加入素材库；如需删除，请从素材库执行删除操作",
        )
    if generation.discarded_at is not None:
        return
    if generation.status in {"queued", "running"}:
        generation.status = "canceled"
        generation.execution_generation += 1
        generation.retryable = False
        generation.finished_at = utcnow()
        generation.lease_owner = None
        generation.lease_expires_at = None
    generation.discarded_at = utcnow()
    if generation.output_storage_path:
        path = Path(generation.output_storage_path).resolve(strict=False)
        root = (settings.data_dir / "private" / "image-generations").resolve()
        if path.is_relative_to(root):
            _delete_after_commit(session, path)
        generation.output_storage_path = None
    _delete_staging_after_commit(session, settings, generation.id)
    add_audit(
        session,
        generation.project_id,
        "image_generation",
        generation.id,
        "image_generation.discarded",
        after={"status": generation.status},
        request_id=request_id,
    )
    session.flush()
