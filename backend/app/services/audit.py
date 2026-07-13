from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuditEvent
from ..serializers import audit_dict


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
