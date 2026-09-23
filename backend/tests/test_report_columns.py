"""The adjudication report is the deliverable, so its shape is asserted.

``generate_csv`` writes the header from ``REPORT_COLUMNS`` and the body from a
separate hand-maintained list of values. Nothing tied the two together: adding
a column to one and not the other does not raise, it shifts every subsequent
value one place left. In a compliance report that means a ``Decision`` printed
under ``Comment / Notes`` and a reviewer reading someone else's rationale
against the wrong alert — silent, and invisible until an audit.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MatchResult, ScreeningRun
from app.report import REPORT_COLUMNS, generate_csv


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _run_with_one_match(db):
    run = ScreeningRun(
        run_ref="test-run-0001",
        origin="batch",
        threshold=80,
        strong_threshold=95,
        uploaded_filename="book.csv",
        watchlist_snapshot={"OFAC SDN": {"payload_sha256": "abc123"}},
    )
    db.add(run)
    db.flush()
    db.add(
        MatchResult(
            run_id=run.id,
            target_name="CABELLO RONDON, Diosdado",
            source="OFAC SDN",
            subject_type="Individual",
            dob="15 Apr 1963",
            nationality="Venezuela (citizen)",
            place_of_birth="Caracas, Venezuela",
            gender="Male",
            identifiers="Cedula No. 8370825 (Venezuela) | Passport A0237802 (Venezuela)",
            listing_reason="Member of the Constituent Assembly",
            linked_to="GOVERNMENT OF VENEZUELA",
            listed_on="2018-05-18",
            geography="Venezuela",
            score=100.0,
            base_score=100.0,
            risk_band="Strong match",
        )
    )
    db.commit()
    return run


def test_header_and_data_row_have_the_same_width() -> None:
    db = _session()
    run = _run_with_one_match(db)

    rows = list(csv.reader(io.StringIO(generate_csv(db, run))))
    header, body = rows[0], rows[1:]

    assert header == REPORT_COLUMNS
    assert body, "a run with one match must produce one data row"
    for index, row in enumerate(body):
        assert len(row) == len(header), (
            f"data row {index} has {len(row)} fields for {len(header)} columns — "
            "every value after the mismatch is written under the wrong heading"
        )


def test_the_identity_record_reaches_the_report() -> None:
    """A dismissal is only defensible against the identity that was on record.

    "Different person" means nothing to a reviewer who cannot see the date of
    birth, nationality and document number the officer compared against.
    """
    db = _session()
    run = _run_with_one_match(db)
    row = dict(zip(REPORT_COLUMNS, list(csv.reader(io.StringIO(generate_csv(db, run))))[1]))

    assert row["DOB"] == "15 Apr 1963"
    assert row["Nationality"] == "Venezuela (citizen)"
    assert row["Place_Of_Birth"] == "Caracas, Venezuela"
    assert "Passport A0237802" in row["Identity_Documents"]
    assert row["Listing_Reason"] == "Member of the Constituent Assembly"
    assert row["Listed_On"] == "2018-05-18"
    # Still aligned at the far end of the row, after the new columns.
    assert row["Risk_Band"] == "Strong match"
    assert row["Source_List_SHA256"] == "abc123"
