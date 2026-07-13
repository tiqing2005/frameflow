from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import APIError
from ..models import Asset, AuditEvent, Job, Project, Segment


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
