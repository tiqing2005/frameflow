from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..llm import suggest_asset_tags_detailed
from ..models import AIRun, Asset, utcnow
from ..nlp import extract_keywords, infer_topic
from ..serializers import asset_dict
from ..vision import suggest_visual_asset_tags
from .common import _get_asset, add_audit, dumps, stable_hash

ASSET_TAGGING_PROMPT_VERSION = "asset-vision-tags-v1"
ASSET_TAGGING_MAX_ATTEMPTS = 3
MAX_NORMALIZED_JPEG_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AssetTaggingClaim:
    asset_id: str
    generation: int
    attempt: int
    worker_id: str


@dataclass(frozen=True, slots=True)
class AssetTaggingSnapshot:
    asset_id: str
    name: str
    kind: str
    storage_path: str | None
    thumbnail_storage_path: str | None
    thumbnail_mime_type: str | None
    tags: list[str]
    keywords: list[str]
    mode: str
    generation: int
    attempt: int


@dataclass(frozen=True, slots=True)
class PreparedFrame:
    jpeg_bytes: bytes | None
    source: str | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class AssetTaggingOutcome:
    tags: list[str]
    keywords: list[str]
    source: str
    provider: str
    model: str
    status: str
    degraded: bool
    duration_ms: int
    input_hash: str
    frame_source: str | None
    stages: list[dict]
    error_message: str | None = None
    usage: dict[str, int] | None = None


def _json_list(value: str) -> list[str]:
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item).strip()]


def _trusted_media_path(raw_path: str | None, settings: Settings) -> Path | None:
    if not raw_path:
        return None
    try:
        path = Path(raw_path).resolve(strict=True)
        roots = (
            (settings.data_dir / "media" / "seed").resolve(strict=True),
            (settings.data_dir / "media" / "uploads" / "assets").resolve(strict=True),
        )
    except OSError:
        return None
    if not path.is_file():
        return None
    if not any(path.is_relative_to(root) for root in roots):
        return None
    return path


def _valid_jpeg_bytes(content: bytes) -> bool:
    return (
        128 <= len(content) <= MAX_NORMALIZED_JPEG_BYTES
        and content.startswith(b"\xff\xd8\xff")
        and content.endswith(b"\xff\xd9")
    )


def _read_poster(snapshot: AssetTaggingSnapshot, settings: Settings) -> bytes | None:
    if snapshot.kind != "video" or snapshot.thumbnail_mime_type != "image/jpeg":
        return None
    poster = _trusted_media_path(snapshot.thumbnail_storage_path, settings)
    if poster is None:
        return None
    try:
        content = poster.read_bytes()
    except OSError:
        return None
    return content if _valid_jpeg_bytes(content) else None


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        # subprocess.run will turn a genuinely missing executable into the
        # normal visual-preprocessing fallback instead of breaking app import.
        return "ffmpeg"


def _normalize_frame(source: Path, settings: Settings) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="frameflow-vision-") as directory:
        target = Path(directory) / "frame.jpg"
        command = [
            _ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe,crypto,data",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            "scale=1280:1280:force_original_aspect_ratio=decrease",
            "-q:v",
            "4",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-threads",
            "1",
            str(target),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=max(1.0, settings.thumbnail_timeout),
                check=False,
            )
            if completed.returncode != 0 or not target.is_file():
                return None
            content = target.read_bytes()
        except (OSError, subprocess.SubprocessError):
            return None
        return content if _valid_jpeg_bytes(content) else None


def prepare_asset_frame(
    snapshot: AssetTaggingSnapshot, settings: Settings
) -> PreparedFrame:
    poster = _read_poster(snapshot, settings)
    if poster is not None:
        return PreparedFrame(poster, "video_poster")
    source = _trusted_media_path(snapshot.storage_path, settings)
    if source is None:
        return PreparedFrame(
            None,
            None,
            "vision_input_unavailable",
            "素材画面不可读取，已转入文本标签降级流程",
        )
    content = _normalize_frame(source, settings)
    if content is None:
        return PreparedFrame(
            None,
            None,
            "vision_preprocessing_failed",
            "素材画面预处理失败，已转入文本标签降级流程",
        )
    return PreparedFrame(content, "video_frame" if snapshot.kind == "video" else "image")


def deterministic_asset_tags(
    name: str, tags: list[str] | None = None, keywords: list[str] | None = None
) -> tuple[list[str], list[str]]:
    context = " ".join([name, *(tags or []), *(keywords or [])]).strip()
    rule_keywords = extract_keywords(context, top_k=8)
    if not rule_keywords:
        rule_keywords = [(name.strip() or "素材")[:20]]
    topic = (infer_topic(context or name, rule_keywords) or "其他")[:20]
    return [topic], rule_keywords[:8]


def _stage(
    stage: str,
    provider: str,
    model: str,
    status: str,
    duration_ms: int,
    error_code: str | None = None,
) -> dict:
    result = {
        "stage": stage,
        "provider": provider,
        "model": model,
        "status": status,
        "duration_ms": duration_ms,
    }
    if error_code:
        result["error_code"] = error_code
    return result


def generate_asset_tagging_outcome(
    snapshot: AssetTaggingSnapshot, settings: Settings
) -> AssetTaggingOutcome:
    started = time.perf_counter()
    stages: list[dict] = []
    frame = PreparedFrame(None, None)
    visual_error: str | None = None

    vision_configured = (
        settings.vision_provider not in {"", "none", "off", "disabled"}
        and bool(settings.vision_api_key)
    )
    if vision_configured:
        frame = prepare_asset_frame(snapshot, settings)
    if frame.jpeg_bytes is not None:
        visual = suggest_visual_asset_tags(frame.jpeg_bytes, settings)
        stages.append(
            _stage(
                "vision",
                visual.provider,
                visual.model,
                visual.status,
                visual.duration_ms,
                visual.error_code,
            )
        )
        visual_error = visual.error_message
        if visual.status == "succeeded" and visual.tags and visual.keywords:
            digest = hashlib.sha256(frame.jpeg_bytes).hexdigest()
            return AssetTaggingOutcome(
                visual.tags,
                visual.keywords,
                "vision",
                visual.provider,
                visual.model,
                "succeeded",
                False,
                max(0, int((time.perf_counter() - started) * 1_000)),
                stable_hash(digest, ASSET_TAGGING_PROMPT_VERSION),
                frame.source,
                stages,
                usage=visual.usage,
            )
    else:
        code = frame.error_code or "vision_not_configured"
        visual_error = frame.error_message or "视觉识别未配置，已转入文本标签降级流程"
        stages.append(
            _stage(
                "vision",
                settings.vision_provider or "none",
                settings.vision_model,
                "degraded",
                0,
                code,
            )
        )

    description_parts = [snapshot.name]
    if snapshot.mode == "fill_missing":
        description_parts.extend(snapshot.tags)
        description_parts.extend(snapshot.keywords)
    text_suggestion = suggest_asset_tags_detailed(
        snapshot.name, " ".join(description_parts), settings
    )
    text_ok = (
        text_suggestion.status == "succeeded"
        and bool(text_suggestion.tags)
        and bool(text_suggestion.keywords)
    )
    text_error_code = None
    if not text_ok:
        text_error_code = (
            "text_llm_not_configured"
            if text_suggestion.provider == "rules" or not settings.llm_api_key
            else "text_llm_failed"
        )
    stages.append(
        _stage(
            "text_llm",
            text_suggestion.provider,
            text_suggestion.model,
            "succeeded" if text_ok else "degraded",
            text_suggestion.duration_ms,
            text_error_code,
        )
    )
    if text_ok:
        return AssetTaggingOutcome(
            text_suggestion.tags,
            text_suggestion.keywords,
            "text_llm",
            text_suggestion.provider,
            text_suggestion.model,
            "degraded",
            True,
            max(0, int((time.perf_counter() - started) * 1_000)),
            stable_hash(snapshot.name, ASSET_TAGGING_PROMPT_VERSION, "text"),
            frame.source,
            stages,
            visual_error or "视觉识别不可用，已根据名称和文本生成标签",
            text_suggestion.usage,
        )

    rule_context_tags = snapshot.tags if snapshot.mode == "fill_missing" else []
    rule_context_keywords = snapshot.keywords if snapshot.mode == "fill_missing" else []
    tags, keywords = deterministic_asset_tags(
        snapshot.name, rule_context_tags, rule_context_keywords
    )
    stages.append(_stage("rules", "rules", "rule-keywords-v1", "succeeded", 0))
    degraded_errors = [
        message
        for message in (visual_error, text_suggestion.error_message)
        if message
    ]
    degraded_errors.append("已使用本地规则生成标签")
    return AssetTaggingOutcome(
        tags,
        keywords,
        "rules",
        "rules",
        "rule-keywords-v1",
        "degraded",
        True,
        max(0, int((time.perf_counter() - started) * 1_000)),
        stable_hash(snapshot.name, ASSET_TAGGING_PROMPT_VERSION, "rules"),
        frame.source,
        stages,
        "；".join(degraded_errors)[:500],
        {},
    )


def asset_tagging_snapshot(
    session: Session, claim: AssetTaggingClaim
) -> AssetTaggingSnapshot | None:
    asset = session.scalar(
        select(Asset).where(
            Asset.id == claim.asset_id,
            Asset.tagging_status == "running",
            Asset.tagging_generation == claim.generation,
            Asset.tagging_attempt == claim.attempt,
            Asset.tagging_lease_owner == claim.worker_id,
        )
    )
    if asset is None:
        return None
    return AssetTaggingSnapshot(
        asset.id,
        asset.name,
        asset.kind,
        asset.storage_path,
        asset.thumbnail_storage_path,
        asset.thumbnail_mime_type,
        _json_list(asset.tags_json),
        _json_list(asset.keywords_json),
        asset.tagging_mode or "fill_missing",
        asset.tagging_generation,
        asset.tagging_attempt,
    )


def _apply_outcome_to_locked_asset(
    session: Session,
    asset: Asset,
    outcome: AssetTaggingOutcome,
    *,
    request_id: str | None = None,
) -> None:
    before = asset_dict(asset)
    current_tags = _json_list(asset.tags_json)
    current_keywords = _json_list(asset.keywords_json)
    applied_fields: list[str] = []
    if asset.tagging_mode == "replace":
        asset.tags_json = dumps(outcome.tags)
        asset.keywords_json = dumps(outcome.keywords)
        applied_fields = ["tags", "keywords"]
    else:
        if not current_tags:
            asset.tags_json = dumps(outcome.tags)
            applied_fields.append("tags")
        if not current_keywords:
            asset.keywords_json = dumps(outcome.keywords)
            applied_fields.append("keywords")

    mode = asset.tagging_mode or "fill_missing"
    asset.tagging_status = outcome.status
    asset.tagging_source = outcome.source
    asset.tagging_mode = None
    asset.tagging_lease_owner = None
    asset.tagging_lease_expires_at = None
    asset.tagging_finished_at = utcnow()
    session.add(
        AIRun(
            operation="asset_tagging",
            provider=outcome.provider,
            model=outcome.model,
            prompt_version=ASSET_TAGGING_PROMPT_VERSION,
            input_hash=outcome.input_hash,
            status=outcome.status,
            degraded=outcome.degraded,
            duration_ms=outcome.duration_ms,
            output_summary_json=dumps(
                {
                    "asset_id": asset.id,
                    "generation": asset.tagging_generation,
                    "attempt": asset.tagging_attempt,
                    "mode": mode,
                    "source": outcome.source,
                    "frame_source": outcome.frame_source,
                    "applied_fields": applied_fields,
                    "generated_tags": outcome.tags,
                    "generated_keywords": outcome.keywords,
                    "stages": outcome.stages,
                    "tokens": outcome.usage or {},
                }
            ),
            error_message=outcome.error_message,
        )
    )
    add_audit(
        session,
        None,
        "asset",
        asset.id,
        "asset.tags_regenerated" if mode == "replace" else "asset.tags_generated",
        before=before,
        after=asset_dict(asset),
        actor="system",
        request_id=request_id,
    )


def apply_asset_tagging_outcome(
    session: Session, claim: AssetTaggingClaim, outcome: AssetTaggingOutcome
) -> bool:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    query = select(Asset).where(
        Asset.id == claim.asset_id,
        Asset.tagging_status == "running",
        Asset.tagging_generation == claim.generation,
        Asset.tagging_attempt == claim.attempt,
        Asset.tagging_lease_owner == claim.worker_id,
    )
    if dialect == "postgresql":
        query = query.with_for_update()
    asset = session.scalar(query)
    if asset is None:
        return False
    _apply_outcome_to_locked_asset(session, asset, outcome)
    return True


def request_asset_retag(
    session: Session, asset_id: str, request_id: str | None
) -> Asset:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    query = select(Asset).where(Asset.id == asset_id)
    if dialect == "postgresql":
        query = query.with_for_update()
    asset = session.scalar(query)
    if asset is None:
        return _get_asset(session, asset_id)
    if asset.tagging_status in {"queued", "running"} and asset.tagging_mode == "replace":
        return asset

    before = asset_dict(asset)
    asset.tagging_generation += 1
    asset.tagging_attempt = 0
    asset.tagging_status = "queued"
    asset.tagging_source = None
    asset.tagging_mode = "replace"
    asset.tagging_lease_owner = None
    asset.tagging_lease_expires_at = None
    asset.tagging_requested_at = utcnow()
    asset.tagging_started_at = None
    asset.tagging_finished_at = None
    session.flush()
    add_audit(
        session,
        None,
        "asset",
        asset.id,
        "asset.tagging_requested",
        before=before,
        after=asset_dict(asset),
        request_id=request_id,
    )
    return asset


def outcome_with_worker_fallback(
    snapshot: AssetTaggingSnapshot, error_code: str, error_message: str
) -> AssetTaggingOutcome:
    tags, keywords = deterministic_asset_tags(
        snapshot.name,
        snapshot.tags if snapshot.mode == "fill_missing" else [],
        snapshot.keywords if snapshot.mode == "fill_missing" else [],
    )
    return AssetTaggingOutcome(
        tags=tags,
        keywords=keywords,
        source="rules",
        provider="rules",
        model="rule-keywords-v1",
        status="degraded",
        degraded=True,
        duration_ms=0,
        input_hash=stable_hash(
            snapshot.name, ASSET_TAGGING_PROMPT_VERSION, "worker-fallback"
        ),
        frame_source=None,
        stages=[
            _stage("worker", "worker", "durable-worker", "degraded", 0, error_code),
            _stage("rules", "rules", "rule-keywords-v1", "succeeded", 0),
        ],
        error_message=error_message,
        usage={},
    )


def finalize_exhausted_asset_tagging(session: Session, asset: Asset) -> None:
    """Finish a repeatedly crashed task locally while its claim row is locked."""
    snapshot = AssetTaggingSnapshot(
        asset.id,
        asset.name,
        asset.kind,
        asset.storage_path,
        asset.thumbnail_storage_path,
        asset.thumbnail_mime_type,
        _json_list(asset.tags_json),
        _json_list(asset.keywords_json),
        asset.tagging_mode or "fill_missing",
        asset.tagging_generation,
        asset.tagging_attempt,
    )
    outcome = outcome_with_worker_fallback(
        snapshot,
        "asset_tagging_attempts_exhausted",
        "后台识别多次中断，已使用本地规则生成标签",
    )
    _apply_outcome_to_locked_asset(session, asset, outcome)
