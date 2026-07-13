from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..errors import APIError
from ..models import Asset, Job, WorkerHeartbeat
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


def ready_payload(session: Session, settings: Settings) -> dict:
    session.execute(text("SELECT 1"))
    asset_count = session.scalar(
        select(func.count()).select_from(Asset).where(Asset.active.is_(True))
    ) or 0
    heartbeat = session.get(WorkerHeartbeat, 1)
    worker_online = False
    worker_state = "dead"
    current_job_id = None
    last_heartbeat = None
    if heartbeat:
        value = heartbeat.heartbeat_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()
        worker_online = age < max(15.0, settings.worker_poll_seconds * 8)
        last_heartbeat = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if worker_online:
            active_job = session.scalar(
                select(Job)
                .where(
                    Job.status == "running",
                    Job.lease_owner == heartbeat.worker_id,
                    Job.lease_expires_at >= datetime.now(timezone.utc),
                )
                .order_by(Job.started_at, Job.created_at)
                .limit(1)
            )
            worker_state = "busy" if active_job else "idle"
            current_job_id = active_job.id if active_job else None
    checks = {
        "database": "ok",
        "seed_assets": {"ok": asset_count >= 12, "count": asset_count},
        "worker": {
            "online": worker_online,
            "state": worker_state,
            "current_job_id": current_job_id,
            "last_heartbeat": last_heartbeat,
        },
    }
    if asset_count < 12 or not worker_online:
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
