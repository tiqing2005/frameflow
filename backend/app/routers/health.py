from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..errors import APIError
from ..models import Asset, Job, WorkerHeartbeat
from ..services.assets import MINIMUM_ACTIVE_ASSETS
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["health"])
root_router = APIRouter(tags=["health"])


def live_payload() -> dict:
    return {
        "status": "ok",
        "service": "frameflow-api",
        "version": __version__,
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ready_payload(session: Session, settings: Settings) -> dict:
    session.execute(text("SELECT 1"))
    asset_count = session.scalar(
        select(func.count()).select_from(Asset).where(Asset.active.is_(True))
    ) or 0
    now = datetime.now(timezone.utc)
    online_cutoff_seconds = max(15.0, settings.worker_poll_seconds * 8)
    heartbeats = session.scalars(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())
    ).all()
    online_heartbeats = [
        heartbeat
        for heartbeat in heartbeats
        if (now - _as_utc(heartbeat.heartbeat_at)).total_seconds()
        < online_cutoff_seconds
    ]
    online_worker_ids = [heartbeat.worker_id for heartbeat in online_heartbeats]
    active_jobs = (
        session.scalars(
            select(Job)
            .where(
                Job.status == "running",
                Job.lease_owner.in_(online_worker_ids),
                Job.lease_expires_at >= now,
            )
            .order_by(Job.started_at, Job.created_at)
        ).all()
        if online_worker_ids
        else []
    )
    active_jobs_by_worker: dict[str, list[Job]] = {}
    for job in active_jobs:
        if job.lease_owner is not None:
            active_jobs_by_worker.setdefault(job.lease_owner, []).append(job)

    worker_instances = []
    for heartbeat in online_heartbeats:
        jobs = active_jobs_by_worker.get(heartbeat.worker_id, [])
        isolated = heartbeat.operational_state == "isolated"
        worker_instances.append(
            {
                "worker_id": heartbeat.worker_id,
                "state": "isolated" if isolated else ("busy" if jobs else "idle"),
                "accepting_jobs": not isolated,
                "detail": heartbeat.status_detail,
                "active_job_ids": [job.id for job in jobs],
                "last_heartbeat": _as_utc(heartbeat.heartbeat_at)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    online_workers = len(online_heartbeats)
    accepting_workers = sum(
        heartbeat.operational_state != "isolated"
        for heartbeat in online_heartbeats
    )
    busy_workers = len(active_jobs_by_worker)
    active_job_ids = [job.id for job in active_jobs]
    isolated_details = [
        heartbeat.status_detail
        for heartbeat in online_heartbeats
        if heartbeat.operational_state == "isolated" and heartbeat.status_detail
    ]
    worker_online = online_workers > 0
    if not worker_online:
        worker_state = "dead"
    elif accepting_workers == 0:
        worker_state = "isolated"
    elif accepting_workers < online_workers:
        worker_state = "degraded"
    elif active_jobs:
        worker_state = "busy"
    else:
        worker_state = "idle"
    last_heartbeat = (
        _as_utc(heartbeats[0].heartbeat_at)
        .isoformat()
        .replace("+00:00", "Z")
        if heartbeats
        else None
    )
    status_detail = "; ".join(isolated_details) or None
    checks = {
        "database": "ok",
        "seed_assets": {
            "ok": asset_count >= MINIMUM_ACTIVE_ASSETS,
            "count": asset_count,
            "minimum": MINIMUM_ACTIVE_ASSETS,
        },
        "worker": {
            "online": worker_online,
            "state": worker_state,
            "accepting_jobs": accepting_workers > 0,
            "detail": status_detail,
            "current_job_id": active_job_ids[0] if active_job_ids else None,
            "last_heartbeat": last_heartbeat,
            "online_workers": online_workers,
            "active_job_ids": active_job_ids,
            "capacity": {
                "configured": settings.worker_concurrency,
                "online": online_workers,
                "accepting": accepting_workers,
                "busy": busy_workers,
                "available": max(0, accepting_workers - busy_workers),
            },
            "instances": worker_instances,
        },
    }
    if asset_count < MINIMUM_ACTIVE_ASSETS or accepting_workers == 0:
        raise APIError(
            503,
            "NOT_READY",
            "服务依赖尚未就绪",
            retryable=True,
            details={"checks": checks},
        )
    return {"status": "ready", "checks": checks}


@router.get("/health/live")
def health_live():
    return live_payload()


@router.get("/health/ready")
def health_ready(session: SessionDep, settings: SettingsDep):
    return ready_payload(session, settings)


@root_router.get("/health/live", include_in_schema=False)
def root_health_live():
    return live_payload()


@root_router.get("/health/ready", include_in_schema=False)
def root_health_ready(session: SessionDep, settings: SettingsDep):
    return ready_payload(session, settings)
