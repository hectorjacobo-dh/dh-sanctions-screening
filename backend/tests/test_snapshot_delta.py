"""What each authority changed when it last published.

An officer opening the dashboard needs to know whether a list moved, and in
which direction. A record count alone cannot tell them: the UN has republished
with an unchanged count and different content — corrected dates of birth, added
aliases — and an added alias can be the difference between a hit and a miss.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest.runner import compute_delta
from app.models import Base, WatchlistRefresh, WatchlistTarget


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


def _snapshot(db, source: str, designations: list[dict]) -> WatchlistRefresh:
    refresh = WatchlistRefresh(source=source, status="success", record_count=len(designations))
    db.add(refresh)
    db.flush()
    for entry in designations:
        db.add(
            WatchlistTarget(
                refresh_id=refresh.id,
                source=source,
                watchlist_id=entry["id"],
                target_name=entry.get("name", "ACME"),
                normalized_name=entry.get("name", "ACME"),
                aliases=entry.get("aliases", ""),
                dob=entry.get("dob", ""),
                nationality=entry.get("nationality", ""),
            )
        )
    db.flush()
    return refresh


def test_additions_and_removals_are_counted(db) -> None:
    before = _snapshot(db, "OFAC SDN", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
    after = _snapshot(db, "OFAC SDN", [{"id": "2"}, {"id": "3"}, {"id": "4"}])

    compute_delta(db, after, before.id)

    assert after.added_count == 1     # 4
    assert after.removed_count == 1   # 1
    assert after.amended_count == 0


def test_an_amendment_is_visible_even_though_the_count_is_unchanged(db) -> None:
    """The failure a record count cannot see.

    Same number of designations, same parties, but one gained an alias. Under a
    count-only comparison this refresh looks like nothing happened, while the
    engine's matching behaviour has materially changed.
    """
    before = _snapshot(db, "UN Sanctions", [{"id": "1"}, {"id": "2", "aliases": ""}])
    after = _snapshot(
        db, "UN Sanctions", [{"id": "1"}, {"id": "2", "aliases": "ABU MUHAMMAD"}]
    )

    compute_delta(db, after, before.id)

    assert (after.added_count, after.removed_count) == (0, 0)
    assert after.amended_count == 1
    assert before.record_count == after.record_count  # a count would show nothing


def test_a_renamed_party_is_one_amendment_not_two_events(db) -> None:
    """Keyed on the regulator's identifier, not the name.

    Keying on the name would report a rename as one deletion plus one unrelated
    addition, which reads as a party being delisted — the opposite of true.
    """
    before = _snapshot(db, "EU Sanctions", [{"id": "EU-1", "name": "ACME TRADING"}])
    after = _snapshot(db, "EU Sanctions", [{"id": "EU-1", "name": "ACME TRADING LLC"}])

    compute_delta(db, after, before.id)

    assert (after.added_count, after.removed_count) == (0, 0)
    assert after.amended_count == 1


def test_identity_changes_count_as_amendments(db) -> None:
    """A corrected date of birth changes how an alert is adjudicated."""
    before = _snapshot(db, "UK Sanctions", [{"id": "1", "dob": "1963"}])
    after = _snapshot(db, "UK Sanctions", [{"id": "1", "dob": "15/04/1963"}])

    compute_delta(db, after, before.id)
    assert after.amended_count == 1


def test_an_unchanged_republication_reports_nothing(db) -> None:
    before = _snapshot(db, "OFAC SDN", [{"id": "1"}, {"id": "2"}])
    after = _snapshot(db, "OFAC SDN", [{"id": "1"}, {"id": "2"}])

    compute_delta(db, after, before.id)
    assert (after.added_count, after.removed_count, after.amended_count) == (0, 0, 0)
    assert after.compared_to_refresh_id == before.id


def test_a_first_load_is_not_reported_as_zero_changes(db) -> None:
    """"Nothing to compare" and "nothing changed" are different statements.

    Both would render as three zeroes, so the absence of a baseline is recorded
    explicitly and the dashboard says so in words.
    """
    first = _snapshot(db, "OFAC SDN", [{"id": "1"}])
    compute_delta(db, first, None)
    assert first.compared_to_refresh_id is None


def test_a_pruned_predecessor_is_not_reported_as_a_mass_addition(db) -> None:
    """Only three snapshots are retained.

    If the predecessor's rows are gone, every designation would look new. That
    would announce "19,365 added" on an ordinary day — alarming and false.
    """
    before = _snapshot(db, "OFAC SDN", [{"id": "1"}, {"id": "2"}])
    after = _snapshot(db, "OFAC SDN", [{"id": "1"}, {"id": "2"}])

    # Simulate pruning: the refresh row survives, its designations do not.
    db.query(WatchlistTarget).filter(WatchlistTarget.refresh_id == before.id).delete()
    db.flush()

    compute_delta(db, after, before.id)
    assert after.compared_to_refresh_id is None
    assert after.added_count == 0


def test_designations_without_an_identifier_fall_back_to_the_name(db) -> None:
    """Not every source populates an id on every row; a delta must still work."""
    before = _snapshot(db, "EU Sanctions", [{"id": "", "name": "ALPHA"}])
    after = _snapshot(
        db, "EU Sanctions", [{"id": "", "name": "ALPHA"}, {"id": "", "name": "BETA"}]
    )

    compute_delta(db, after, before.id)
    assert after.added_count == 1
    assert after.removed_count == 0
