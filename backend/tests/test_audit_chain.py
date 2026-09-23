"""Proves the audit chain actually detects tampering.

This is the test that backs the regulatory claim. If it passes, an altered or
deleted historic record is provably detectable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.audit import record_event, verify_chain
from app.models import AuditEvent, Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_chain_is_valid_when_untouched(db) -> None:
    for i in range(5):
        record_event(db, "TEST_EVENT", actor="tester", payload={"i": i})

    result = verify_chain(db)
    assert result["valid"] is True
    assert result["events_checked"] == 5


def test_chain_links_each_record_to_its_predecessor(db) -> None:
    first = record_event(db, "FIRST")
    second = record_event(db, "SECOND")
    assert second.previous_hash == first.record_hash


def test_altered_payload_is_detected(db) -> None:
    for i in range(5):
        record_event(db, "TEST_EVENT", payload={"i": i})

    # Simulate someone editing a historic decision directly in the database.
    target = db.query(AuditEvent).filter(AuditEvent.sequence == 3).one()
    target.payload = {"i": "tampered"}
    db.commit()

    result = verify_chain(db)
    assert result["valid"] is False
    assert result["broken_at"] == 3


def test_deleted_record_is_detected(db) -> None:
    for i in range(5):
        record_event(db, "TEST_EVENT", payload={"i": i})

    db.query(AuditEvent).filter(AuditEvent.sequence == 3).delete()
    db.commit()

    result = verify_chain(db)
    assert result["valid"] is False
    assert result["broken_at"] == 4


def test_empty_chain_is_valid(db) -> None:
    assert verify_chain(db)["valid"] is True
