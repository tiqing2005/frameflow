from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import event, func, or_, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import APIError
from ..llm import suggest_asset_tags_detailed
from ..models import AIRun, Asset, Selection
from ..nlp import extract_keywords, infer_topic
from ..schemas import AssetPatch
from ..serializers import asset_dict
from ..thumbnails import ThumbnailResult, materialize_video_thumbnail
from .common import _get_asset, add_audit, dumps, stable_hash, stream_upload_to_path
from .projects import _delete_after_commit

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

MINIMUM_ACTIVE_ASSETS = 3


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


def create_asset(
    session: Session,
    settings: Settings,
    filename: str,
    content_type: str | None,
    upload: BinaryIO,
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
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    path = settings.data_dir / "media" / "uploads" / "assets" / stored_name
    size_bytes, _sha256, head = stream_upload_to_path(
        upload, path, settings.max_upload_bytes
    )
    if not _valid_asset_signature(head, suffix):
        path.unlink(missing_ok=True)
        raise APIError(415, "ASSET_SIGNATURE_MISMATCH", "素材内容与扩展名不匹配；用户上传 SVG 已禁用以避免脚本风险")
    public_url = f"/media/uploads/assets/{stored_name}"
    mime_type = mimetypes.guess_type(safe_name)[0] or content_type or "application/octet-stream"
    if kind == "video":
        poster_name = f"{Path(stored_name).stem}-poster.jpg"
        poster_path = path.with_name(poster_name)
        thumbnail = materialize_video_thumbnail(
            path,
            poster_path,
            f"/media/uploads/assets/{poster_name}",
            settings,
        )
    else:
        thumbnail = ThumbnailResult(public_url, str(path), mime_type, generated=False)

    owned_paths = {path}
    if thumbnail.storage_path and Path(thumbnail.storage_path) != path:
        owned_paths.add(Path(thumbnail.storage_path))

    def cleanup_after_rollback(_session: Session) -> None:
        for owned_path in owned_paths:
            owned_path.unlink(missing_ok=True)

    event.listen(session, "after_rollback", cleanup_after_rollback, once=True)
    try:
        tag_values = _parse_csv(tags)
        keyword_values = _parse_csv(keywords)
        suggestion = None
        if not tag_values or not keyword_values:
            # Auto-suggest tags/keywords via the LLM when configured; falls back
            # to rule extraction on any failure (degrades to current behavior).
            suggestion = suggest_asset_tags_detailed(name, " ".join(tag_values) or name, settings)
            suggested_tags, suggested_keywords = suggestion.tags, suggestion.keywords
            if not tag_values:
                tag_values = suggested_tags
            if not keyword_values:
                keyword_values = suggested_keywords or extract_keywords(name + " " + " ".join(tag_values))
            if not tag_values:
                # Keep zero-key/offline mode genuinely useful: the deterministic
                # fallback must produce both searchable keywords and at least one
                # topic tag, not an empty tag list advertised as auto-tagging.
                tag_values = [infer_topic(name, keyword_values)]
        asset = Asset(
            name=name,
            kind=kind,
            public_url=public_url,
            storage_path=str(path),
            thumbnail_url=thumbnail.url,
            thumbnail_storage_path=thumbnail.storage_path,
            thumbnail_mime_type=thumbnail.mime_type,
            mime_type=mime_type,
            size_bytes=size_bytes,
            tags_json=dumps(tag_values),
            keywords_json=dumps(keyword_values),
            is_seed=False,
            active=True,
        )
        session.add(asset)
        session.flush()
        if suggestion is not None:
            session.add(
                AIRun(
                    operation="asset_tagging",
                    provider=suggestion.provider,
                    model=suggestion.model,
                    prompt_version="asset-tags-v1",
                    input_hash=stable_hash(name, tags, keywords, "asset-tags-v1"),
                    status=suggestion.status,
                    degraded=suggestion.degraded,
                    duration_ms=suggestion.duration_ms,
                    output_summary_json=dumps(
                        {
                            "asset_id": asset.id,
                            "generated_tags": tag_values,
                            "generated_keywords": keyword_values,
                            "tokens": suggestion.usage or {},
                            "fallback": suggestion.degraded,
                        }
                    ),
                    error_message=suggestion.error_message,
                )
            )
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
        for owned_path in owned_paths:
            owned_path.unlink(missing_ok=True)
        raise


def patch_asset(
    session: Session, asset_id: str, payload: AssetPatch, request_id: str | None
) -> Asset:
    if payload.active is False and session.get_bind().dialect.name == "sqlite":
        # Serialize against manual selection writes so an asset cannot become
        # inactive between selection validation and persistence.
        session.execute(text("BEGIN IMMEDIATE"))
    asset = _get_asset(session, asset_id)
    before = asset_dict(asset)
    if payload.name is not None:
        asset.name = payload.name
    if payload.tags is not None:
        asset.tags_json = dumps(payload.tags)
    if payload.keywords is not None:
        asset.keywords_json = dumps(payload.keywords)
    if payload.active is not None:
        if asset.active and not payload.active:
            selection_count = session.scalar(
                select(func.count()).select_from(Selection).where(Selection.asset_id == asset.id)
            ) or 0
            if selection_count:
                raise APIError(
                    409,
                    "ASSET_IN_USE",
                    "该素材仍被项目片段使用，请先替换相关片段的素材",
                    details={"asset_id": asset.id, "selection_count": selection_count},
                )
            active_count = session.scalar(
                select(func.count()).select_from(Asset).where(Asset.active.is_(True))
            ) or 0
            if active_count <= MINIMUM_ACTIVE_ASSETS:
                raise APIError(
                    409,
                    "MINIMUM_ASSET_GUARD",
                    f"至少保留 {MINIMUM_ACTIVE_ASSETS} 个启用素材",
                    details={
                        "minimum_active_assets": MINIMUM_ACTIVE_ASSETS,
                        "active_assets": active_count,
                    },
                )
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


def delete_asset(
    session: Session,
    settings: Settings,
    asset_id: str,
    request_id: str | None,
) -> None:
    """Delete a user-uploaded asset and its owned media files safely.

    Seed media is immutable and selected assets cannot be removed behind the
    workbench's back.  Files are unlinked only after the database transaction
    commits, so a failed delete never leaves the database pointing at missing
    media.
    """
    if session.get_bind().dialect.name == "sqlite":
        # Serialize with selection writes and active-asset updates.
        session.execute(text("BEGIN IMMEDIATE"))
    asset = _get_asset(session, asset_id)
    if asset.is_seed:
        raise APIError(409, "SEED_ASSET_PROTECTED", "内置演示素材不可删除，请使用用户上传的素材")

    selection_count = session.scalar(
        select(func.count()).select_from(Selection).where(Selection.asset_id == asset.id)
    ) or 0
    if selection_count:
        raise APIError(
            409,
            "ASSET_IN_USE",
            "该素材仍被项目片段使用，请先替换相关片段的素材",
            details={"asset_id": asset.id, "selection_count": selection_count},
        )

    if asset.active:
        active_count = session.scalar(
            select(func.count()).select_from(Asset).where(Asset.active.is_(True))
        ) or 0
        if active_count <= MINIMUM_ACTIVE_ASSETS:
            raise APIError(
                409,
                "MINIMUM_ASSET_GUARD",
                f"至少保留 {MINIMUM_ACTIVE_ASSETS} 个启用素材",
                details={
                    "minimum_active_assets": MINIMUM_ACTIVE_ASSETS,
                    "active_assets": active_count,
                },
            )

    before = asset_dict(asset)
    # Restrict cleanup to the upload directory.  This prevents malformed or
    # legacy database values from turning an API request into arbitrary file
    # deletion, while still removing both source and generated poster files.
    upload_root = (settings.data_dir / "media" / "uploads" / "assets").resolve()
    owned_paths: set[Path] = set()
    for raw_path in (asset.storage_path, asset.thumbnail_storage_path):
        if not raw_path:
            continue
        path = Path(raw_path).resolve(strict=False)
        if path != upload_root and path.is_relative_to(upload_root):
            owned_paths.add(path)

    add_audit(
        session,
        None,
        "asset",
        asset.id,
        "asset.deleted",
        before=before,
        request_id=request_id,
    )
    session.delete(asset)
    session.flush()
    for path in owned_paths:
        _delete_after_commit(session, path)
