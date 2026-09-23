"""Audit trail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import verify_chain
from ..auth import current_user
from ..database import get_db
from ..models import AuditEvent, User
from ..schemas import AuditEventOut, ChainVerification

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/events", response_model=list[AuditEventOut])
def list_events(
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    action: str | None = None,
    entity_ref: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[AuditEvent]:
    filters = []
    if action:
        filters.append(AuditEvent.action == action)
    if entity_ref:
        filters.append(AuditEvent.entity_ref == entity_ref)

    return list(
        db.execute(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.sequence.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


@router.get("/verify", response_model=ChainVerification)
def verify(
    db: Session = Depends(get_db), _: User = Depends(current_user)
) -> ChainVerification:
    """Recompute the hash chain end to end and report any tampering."""
    return ChainVerification(**verify_chain(db))


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    rows = db.execute(
        select(AuditEvent.action, func.count(AuditEvent.id)).group_by(AuditEvent.action)
    ).all()
    total = db.execute(select(func.count(AuditEvent.id))).scalar_one()
    return {
        "total_events": total,
        "by_action": [{"action": action, "count": count} for action, count in rows],
    }
