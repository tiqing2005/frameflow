from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import (
    AIRun,
    Asset,
    AuditEvent,
    Job,
    JobEvent,
    Project,
    Recommendation,
    Segment,
    Selection,
    Source,
)


def loads(value: str | None, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def project_dict(project: Project, segment_count: int | None = None) -> dict[str, Any]:
    data = {
        "id": project.id,
        "title": project.title,
        "status": project.status,
        "input_kind": project.input_kind,
        "input_type": project.input_kind,
        "created_at": iso(project.created_at),
        "updated_at": iso(project.updated_at),
    }
    if segment_count is not None:
        data["segment_count"] = segment_count
    return data


def source_dict(source: Source | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "id": source.id,
        "project_id": source.project_id,
        "kind": source.kind,
        "original_filename": source.original_filename,
        "filename": source.original_filename,
        "url": source.public_url,
        "file_url": source.public_url,
        "mime_type": source.mime_type,
        "size_bytes": source.size_bytes,
        "sha256": source.sha256,
        "raw_text": source.content if source.kind == "text" else None,
        "transcript_text": source.transcript_text,
        "text": source.content if source.kind == "text" else None,
        "transcript": source.transcript_text,
        "created_at": iso(source.created_at),
    }


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retryable": job.retryable,
        "created_at": iso(job.created_at),
        "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at),
        "updated_at": iso(job.updated_at),
    }


def event_dict(event: JobEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "stage": event.stage,
        "progress": event.progress,
        "message": event.message,
        "level": event.level,
        "created_at": iso(event.created_at),
    }


def asset_dict(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "name": asset.name,
        "kind": asset.kind,
        "url": asset.public_url,
        "file_url": asset.public_url,
        "thumbnail_url": asset.public_url,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "tags": loads(asset.tags_json, []),
        "keywords": loads(asset.keywords_json, []),
        "is_seed": asset.is_seed,
        "active": asset.active,
        "created_at": iso(asset.created_at),
        "updated_at": iso(asset.updated_at),
    }


def recommendation_dict(recommendation: Recommendation, asset: Asset) -> dict[str, Any]:
    return {
        "id": recommendation.id,
        "segment_id": recommendation.segment_id,
        "asset_id": recommendation.asset_id,
        "asset": asset_dict(asset),
        "rank": recommendation.rank,
        "total_score": round(recommendation.total_score, 6),
        "score": round(recommendation.total_score, 6),
        "tfidf_score": round(recommendation.tfidf_score, 6),
        "keyword_score": round(recommendation.keyword_score, 6),
        "tag_score": round(recommendation.tag_score, 6),
        "matched_terms": loads(recommendation.matched_terms_json, []),
        "explanation": recommendation.explanation,
        "is_diversity_filler": recommendation.is_diversity_filler,
        "created_at": iso(recommendation.created_at),
    }


def selection_dict(selection: Selection, asset: Asset) -> dict[str, Any]:
    return {
        "id": selection.id,
        "segment_id": selection.segment_id,
        "asset_id": selection.asset_id,
        "source": selection.source,
        "asset": asset_dict(asset),
        "created_at": iso(selection.created_at),
        "updated_at": iso(selection.updated_at),
    }


def segment_base_dict(segment: Segment) -> dict[str, Any]:
    return {
        "id": segment.id,
        "project_id": segment.project_id,
        "position": segment.position,
        "order": segment.position,
        "text": segment.text,
        "topic": segment.topic,
        "keywords": loads(segment.keywords_json, []),
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "version": segment.version,
        "created_at": iso(segment.created_at),
        "updated_at": iso(segment.updated_at),
    }


def audit_dict(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "project_id": event.project_id,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "action": event.action,
        "summary": event.action,
        "before": loads(event.before_json, None),
        "after": loads(event.after_json, None),
        "details": loads(event.after_json, {}),
        "actor": event.actor,
        "request_id": event.request_id,
        "created_at": iso(event.created_at),
    }


def run_dict(run: AIRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "job_id": run.job_id,
        "segment_id": run.segment_id,
        "operation": run.operation,
        "provider": run.provider,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "input_hash": run.input_hash,
        "status": run.status,
        "degraded": run.degraded,
        "duration_ms": run.duration_ms,
        "latency_ms": run.duration_ms,
        "output_summary": loads(run.output_summary_json, {}),
        "error_message": run.error_message,
        "created_at": iso(run.created_at),
    }
