"""Archive and remove all partner data, leaving only the public watchlists.

    python purge_partner_data.py --archive-only    # dry run: archive, delete nothing
    python purge_partner_data.py --confirm         # archive, then delete

Removes: screening runs (cases), match results, adjudications, evidence
documents and the audit chain.
Keeps: watchlist designations (published government data) and user accounts.

Why the audit chain goes too
---------------------------
Audit events deliberately record partner names — that is what makes them
evidence of who decided what about whom. They cannot be stripped of partner
data and still mean anything, and editing them would break the hash chain by
design. So the chain is archived in full and restarted, with its first new
event recording that this purge happened, by whom, and where the archive went.
That keeps the purge itself accountable.

Nothing is deleted before the archive has been written and read back.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import delete, func, select  # noqa: E402

from app.audit import record_event  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    EvidenceDocument,
    MatchResult,
    ScreeningRun,
    WatchlistTarget,
)


def _serialise(row: object) -> dict:
    """Row -> plain dict, with timestamps as ISO strings."""
    out: dict = {}
    for column in row.__table__.columns:  # type: ignore[attr-defined]
        value = getattr(row, column.name)
        if isinstance(value, (dt.datetime, dt.date)):
            value = value.isoformat()
        out[column.name] = value
    return out


def archive(db, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)

    runs = db.execute(select(ScreeningRun)).scalars().all()
    matches = db.execute(select(MatchResult)).scalars().all()
    documents = db.execute(select(EvidenceDocument)).scalars().all()
    events = db.execute(select(AuditEvent).order_by(AuditEvent.sequence)).scalars().all()

    payload = {
        "archived_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "counts": {
            "screening_runs": len(runs),
            "match_results": len(matches),
            "evidence_documents": len(documents),
            "audit_events": len(events),
            "adjudicated_decisions": sum(1 for m in matches if m.decision),
        },
        "screening_runs": [_serialise(r) for r in runs],
        "match_results": [_serialise(m) for m in matches],
        "evidence_documents": [_serialise(d) for d in documents],
        "audit_events": [_serialise(e) for e in events],
    }

    data_file = destination / "partner-data-archive.json"
    data_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # Copy the evidence files themselves, not just their metadata.
    evidence_source = get_settings().data_dir / "evidence"
    copied = 0
    if evidence_source.is_dir():
        evidence_destination = destination / "evidence"
        evidence_destination.mkdir(exist_ok=True)
        for item in evidence_source.iterdir():
            if item.is_file():
                shutil.copy2(item, evidence_destination / item.name)
                copied += 1

    # Read the archive back before anything is deleted.
    verified = json.loads(data_file.read_text(encoding="utf-8"))
    assert verified["counts"] == payload["counts"], "archive did not read back intact"

    digest = hashlib.sha256(data_file.read_bytes()).hexdigest()
    (destination / "ARCHIVE-SHA256.txt").write_text(
        f"{digest}  partner-data-archive.json\n", encoding="utf-8"
    )

    return {
        **payload["counts"],
        "evidence_files_copied": copied,
        "archive_sha256": digest,
        "archive_path": str(data_file),
    }


def purge(db, summary: dict, archive_path: str) -> None:
    # Children first, then parents.
    db.execute(delete(EvidenceDocument))
    db.execute(delete(MatchResult))
    db.execute(delete(ScreeningRun))
    db.execute(delete(AuditEvent))
    db.commit()

    # Remove the stored evidence files now that they are archived.
    evidence_dir = get_settings().data_dir / "evidence"
    removed = 0
    if evidence_dir.is_dir():
        for item in evidence_dir.iterdir():
            if item.is_file():
                item.unlink()
                removed += 1

    # First event of the new chain: the purge itself.
    record_event(
        db,
        "PARTNER_DATA_PURGED",
        actor="operator",
        entity_ref="all",
        payload={
            "reason": "Operator elected to retain public sanctions data only.",
            "archived_to": archive_path,
            "archive_sha256": summary["archive_sha256"],
            "removed": {
                "screening_runs": summary["screening_runs"],
                "match_results": summary["match_results"],
                "evidence_documents": summary["evidence_documents"],
                "audit_events_archived": summary["audit_events"],
                "evidence_files_deleted": removed,
            },
            "note": (
                "The previous audit chain was archived in full and restarted. "
                "Audit events record partner names by design and cannot be "
                "stripped while remaining evidence."
            ),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually delete")
    parser.add_argument("--archive-only", action="store_true", help="archive, delete nothing")
    args = parser.parse_args()

    if not (args.confirm or args.archive_only):
        parser.error("pass --archive-only for a dry run, or --confirm to delete")

    settings = get_settings()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H%M%SZ")
    destination = settings.data_dir.parent / "archive" / f"partner-data-{stamp}"

    db = SessionLocal()
    try:
        designations = db.execute(select(func.count(WatchlistTarget.id))).scalar_one()
        summary = archive(db, destination)

        print("ARCHIVED")
        for key in ("screening_runs", "match_results", "adjudicated_decisions",
                    "evidence_documents", "audit_events", "evidence_files_copied"):
            print(f"  {key:<24} {summary[key]:>6}")
        print(f"  archive                  {summary['archive_path']}")
        print(f"  sha256                   {summary['archive_sha256'][:32]}...")

        if args.archive_only:
            print("\nDRY RUN — nothing deleted.")
            return 0

        purge(db, summary, summary["archive_path"])

        remaining = {
            "screening_runs": db.execute(select(func.count(ScreeningRun.id))).scalar_one(),
            "match_results": db.execute(select(func.count(MatchResult.id))).scalar_one(),
            "evidence_documents": db.execute(
                select(func.count(EvidenceDocument.id))
            ).scalar_one(),
            "audit_events": db.execute(select(func.count(AuditEvent.id))).scalar_one(),
            "watchlist_designations": db.execute(
                select(func.count(WatchlistTarget.id))
            ).scalar_one(),
        }
        print("\nPURGED — remaining in the database:")
        for key, value in remaining.items():
            print(f"  {key:<24} {value:>8,}")
        assert remaining["match_results"] == 0, "partner data still present"
        assert remaining["audit_events"] == 1, "new chain should hold exactly the purge event"
        assert remaining["watchlist_designations"] == designations, "watchlists were harmed"
        print("\nPublic sanctions data untouched. New audit chain starts with the purge record.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
