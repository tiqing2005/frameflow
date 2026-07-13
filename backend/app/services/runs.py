from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AIRun
from ..serializers import run_dict


def list_runs(session: Session, limit: int = 100, offset: int = 0) -> dict:
    total = session.scalar(select(func.count()).select_from(AIRun)) or 0
    runs = session.scalars(
        select(AIRun).order_by(AIRun.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {"items": [run_dict(run) for run in runs], "total": total}
