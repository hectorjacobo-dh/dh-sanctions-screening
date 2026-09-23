"""Hash-chained audit log.

Writing an audit event is serialised through a lock and a single transaction so
that the sequence number and the chain hash stay consistent under concurrent
requests.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AuditEvent, utcnow

_chain_lock = threading.Lock()

GENESIS_HASH = "0" * 64


def _utc_isoformat(timestamp: dt.datetime) -> str:
    """Render a timestamp identically at write time and at verification time.

    Timestamps are written as timezone-aware UTC, but SQLite does not persist
    tzinfo, so they are read back naive. Calling ``astimezone`` on a naive value
    makes Python assume *local* time, which shifts the string by the UTC offset
    and breaks every hash on read-back. Naive values are therefore treated as
    the UTC they already are, rather than converted.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.UTC)
    return timestamp.astimezone(dt.UTC).isoformat(timespec="microseconds")


def canonical_payload(
    sequence: int,
    timestamp: dt.datetime,
    actor: str,
    action: str,
    entity_ref: str,
    payload: dict[str, Any],
    previous_hash: str,
) -> str:
    """Deterministic serialisation — key order and separators are fixed.

    Any change here invalidates historic chains, so it must stay stable.
    """
    return json.dumps(
        {
            "sequence": sequence,
            "timestamp_utc": _utc_isoformat(timestamp),
            "actor": actor,
            "action": action,
            "entity_ref": entity_ref,
            "payload": payload,
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_hash(serialised: str) -> str:
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def record_event(
    db: Session,
    action: str,
    *,
    actor: str = "system",
    entity_ref: str = "",
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditEvent:
    """Append one tamper-evident event."""
    payload = payload or {}
    with _chain_lock:
        last = db.execute(
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        ).scalar_one_or_none()

        sequence = (last.sequence + 1) if last else 1
        previous_hash = last.record_hash if last else GENESIS_HASH
        timestamp = utcnow()

        serialised = canonical_payload(
            sequence, timestamp, actor, action, entity_ref, payload, previous_hash
        )
        event = AuditEvent(
            sequence=sequence,
            timestamp_utc=timestamp,
            actor=actor,
            action=action,
            entity_ref=entity_ref,
            payload=payload,
            previous_hash=previous_hash,
            record_hash=compute_hash(serialised),
        )
        db.add(event)
        if commit:
            db.commit()
        else:
            db.flush()
        return event


def verify_chain(db: Session) -> dict[str, Any]:
    """Recompute the whole chain and report the first inconsistency, if any."""
    total = db.execute(select(func.count(AuditEvent.id))).scalar_one()
    if total == 0:
        return {"valid": True, "events_checked": 0, "broken_at": None, "detail": "empty chain"}

    expected_previous = GENESIS_HASH
    expected_sequence = 1
    checked = 0

    stmt = select(AuditEvent).order_by(AuditEvent.sequence.asc()).execution_options(yield_per=500)
    for event in db.execute(stmt).scalars():
        if event.sequence != expected_sequence:
            return {
                "valid": False,
                "events_checked": checked,
                "broken_at": event.sequence,
                "detail": f"sequence gap: expected {expected_sequence}, found {event.sequence}",
            }
        if event.previous_hash != expected_previous:
            return {
                "valid": False,
                "events_checked": checked,
                "broken_at": event.sequence,
                "detail": "previous_hash does not match prior record",
            }
        recomputed = compute_hash(
            canonical_payload(
                event.sequence,
                event.timestamp_utc,
                event.actor,
                event.action,
                event.entity_ref,
                event.payload or {},
                event.previous_hash,
            )
        )
        if recomputed != event.record_hash:
            return {
                "valid": False,
                "events_checked": checked,
                "broken_at": event.sequence,
                "detail": "record contents were altered after write",
            }
        expected_previous = event.record_hash
        expected_sequence += 1
        checked += 1

    return {
        "valid": True,
        "events_checked": checked,
        "broken_at": None,
        "detail": "chain intact",
        "head_hash": expected_previous,
    }
