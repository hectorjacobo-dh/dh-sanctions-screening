"""Export the active watchlist snapshot and reason taxonomy for the artifact.

Published government designations only: no partner data, no screening results,
no adjudications. Records are arrays, not objects, to strip key overhead.
Layout: [name, sourceIdx, typeChar, programme, geography, watchlistId, aliases, dob]
"""
import json, pathlib, re, sys

from sqlalchemy import select

from app.database import SessionLocal
from app.dismissal import DECISIONS_REQUIRING_REASON, REASONS, REASONS_REQUIRING_RATIONALE
from app.ingest.runner import active_refresh_ids
from app.models import WatchlistTarget

SOURCES = ["OFAC SDN", "OFAC Non-SDN", "EU Sanctions", "UK Sanctions", "UN Sanctions"]


def clean(value, limit):
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    db = SessionLocal()
    index = {s: i for i, s in enumerate(SOURCES)}
    rows = db.execute(
        select(WatchlistTarget).where(WatchlistTarget.refresh_id.in_(active_refresh_ids(db)))
    ).scalars().all()

    records, seen = [], set()
    for row in rows:
        name = clean(row.target_name, 96)
        key = (name.casefold(), row.source)
        if not name or key in seen:
            continue
        seen.add(key)
        records.append([
            name,
            index.get(row.source, 0),
            (row.subject_type or "Unknown")[0],
            clean(row.sanctions_program, 40),
            clean(row.geography, 26),
            clean(row.watchlist_id, 14),
            clean(row.aliases, 44),
            clean(row.dob, 12),
        ])

    (out_dir / "designations_full.json").write_text(
        json.dumps({"src": SOURCES, "rec": records}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (out_dir / "taxonomy.json").write_text(
        json.dumps({
            "reasons": [vars(r) for r in REASONS],
            "requires_rationale": sorted(REASONS_REQUIRING_RATIONALE),
            "decisions_requiring_reason": sorted(DECISIONS_REQUIRING_REASON),
        }, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"exported {len(records):,} designations and {len(REASONS)} reason codes")


if __name__ == "__main__":
    main()
