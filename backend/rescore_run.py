"""Re-score a finished run's alerts with the current matching logic.

    python rescore_run.py --run-id 9              # dry run: report, change nothing
    python rescore_run.py --run-id 9 --apply      # archive removed alerts, then delete them

Why this exists
---------------
A screening run of 83,642 names takes hours. When the matching logic is later
found to be producing false alerts, re-running the whole file to clear them is
disproportionate: every alert already carries the partner name, the designation
it was matched to, and the country, so each pair can simply be scored again.

Why that is sound
-----------------
Re-scoring can only REMOVE alerts, never add them. A pair that was not an alert
before was below threshold under the old logic, and the corrections made since
only make scoring stricter (acronyms are no longer bridged to individuals,
repeated words no longer count as a discriminative name, very short names are
held below the alerting range). So nothing that was missed becomes found, and
nothing that is found now was absent from the original run. If a future change
makes scoring MORE permissive, this tool is the wrong instrument and the run
has to be repeated.

What it will never touch
------------------------
* an alert a person has already resolved;
* an alert whose designation has since left the watchlist and so cannot be
  re-scored — it is kept and counted, because absence of evidence is not a
  reason to discard a result.

Accountability
--------------
Removed alerts are written to a CSV first (kept out of git: it holds partner
names), and the removal is recorded in the audit chain with the counts and the
SHA-256 of that archive. Removing alerts is a decision about a control, and the
record has to show that it was made, when, and on what basis.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import delete, select  # noqa: E402

from app import matching  # noqa: E402
from app.audit import record_event  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.ingest.runner import active_refresh_ids  # noqa: E402
from app.matching import band_for, score_against_all_names  # noqa: E402
from app.models import MatchResult, ScreeningRun, WatchlistTarget, utcnow  # noqa: E402

ARCHIVE_COLUMNS = [
    "id", "grid", "partner_legal_name", "account_country", "target_name", "matched_alias",
    "source", "watchlist_id", "subject_type", "score", "base_score", "risk_band",
    "reason_codes", "new_base_score", "new_reasons",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="archive and delete (default: dry run)")
    parser.add_argument(
        "--acronym-min-batch", type=int, default=None,
        help="evaluate a different batch acronym length without editing the code",
    )
    args = parser.parse_args()

    if args.acronym_min_batch is not None:
        matching.ACRONYM_MIN_LEN_BATCH = args.acronym_min_batch

    db = SessionLocal()
    run = db.get(ScreeningRun, args.run_id)
    if run is None:
        raise SystemExit(f"no run with id {args.run_id}")
    if run.status != "completed":
        raise SystemExit(f"run {run.id} is '{run.status}'; re-score only a completed run")

    ids = active_refresh_ids(db)
    targets = {
        (t.source, t.watchlist_id): t
        for t in db.execute(
            select(WatchlistTarget).where(WatchlistTarget.refresh_id.in_(ids))
        ).scalars()
    }
    print(f"run {run.id}: {run.uploaded_filename}  threshold={run.threshold}  "
          f"acronym minimum (batch)={matching.ACRONYM_MIN_LEN_BATCH}")
    print(f"active designations available to re-score against: {len(targets):,}")

    alerts = db.execute(select(MatchResult).where(MatchResult.run_id == run.id)).scalars().all()
    kept, removed, protected, orphaned = [], [], 0, 0
    removal_reason: collections.Counter = collections.Counter()
    kept_reason: collections.Counter = collections.Counter()

    for alert in alerts:
        if alert.decided_at is not None:
            protected += 1
            continue
        target = targets.get((alert.source, alert.watchlist_id))
        if target is None:
            orphaned += 1
            continue

        breakdown, matched = score_against_all_names(
            alert.partner_legal_name,
            target.target_name,
            target.aliases,
            alert.account_country,
            target.geography,
            geo_penalty=run.geo_penalty,
            target_subject_type=target.subject_type,
        )
        if breakdown.base_score >= run.threshold:
            kept.append((alert, breakdown, matched))
            for code in breakdown.reasons:
                kept_reason[str(getattr(code, "value", code))] += 1
        else:
            removed.append((alert, breakdown))
            for code in (alert.reason_codes or "").split(","):
                if code in ("ACRONYM_EXPANSION", "DISCRIMINATIVE_NAME_CONTAINED",
                            "SHORT_NAME_LOW_CONFIDENCE", "MATCHED_ON_ALIAS"):
                    removal_reason[code] += 1

    total = len(alerts)
    print(f"\nalerts in the run            : {total:,}")
    print(f"  resolved by a person (kept)  : {protected:,}")
    print(f"  designation gone (kept)      : {orphaned:,}")
    print(f"  still alerts under new logic : {len(kept):,}")
    print(f"  would be removed             : {len(removed):,}  ({len(removed)/max(total,1):.0%})")
    print("\nremoved alerts carried these old reasons (an alert can carry several):")
    for code, n in removal_reason.most_common():
        print(f"    {code:34s}{n:>7,}")
    print("\nsurviving alerts, by reason:")
    for code, n in kept_reason.most_common(8):
        print(f"    {code:34s}{n:>7,}")
    survivors_acr = sum(1 for _, b, _ in kept if any("ACRONYM" in str(getattr(r, 'value', r)) for r in b.reasons))
    print(f"\nsurvivors that rely on the acronym bridge: {survivors_acr:,}")

    if not args.apply:
        print("\nDRY RUN — nothing was changed. Re-run with --apply to archive and remove.")
        return 0

    # ---- archive first, delete only once the archive is written and re-read ----
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    archive = Path(__file__).resolve().parent.parent / "data" / f"rescore-removed-run{run.id}-{stamp}.csv"
    with archive.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ARCHIVE_COLUMNS)
        writer.writeheader()
        for alert, breakdown in removed:
            writer.writerow({
                "id": alert.id, "grid": alert.grid, "partner_legal_name": alert.partner_legal_name,
                "account_country": alert.account_country, "target_name": alert.target_name,
                "matched_alias": alert.matched_alias, "source": alert.source,
                "watchlist_id": alert.watchlist_id, "subject_type": alert.subject_type,
                "score": alert.score, "base_score": alert.base_score, "risk_band": alert.risk_band,
                "reason_codes": alert.reason_codes,
                "new_base_score": round(breakdown.base_score, 2),
                "new_reasons": ",".join(str(getattr(r, "value", r)) for r in breakdown.reasons),
            })
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with archive.open(encoding="utf-8", newline="") as handle:
        archived_rows = sum(1 for _ in csv.reader(handle)) - 1
    if archived_rows != len(removed):
        raise SystemExit(f"archive read back {archived_rows} rows, expected {len(removed)}; nothing deleted")
    print(f"\narchive written and verified: {archive.name}  ({archived_rows:,} rows)")

    # ---- refresh the survivors so what is stored matches the logic now in force ----
    for alert, breakdown, matched in kept:
        alert.base_score = breakdown.base_score
        alert.geo_adjustment = breakdown.geo_adjustment
        alert.score = breakdown.final_score
        alert.risk_band = str(band_for(breakdown, run.threshold, run.strong_threshold))
        alert.score_breakdown = breakdown.as_dict()
        alert.reason_codes = ",".join(str(getattr(r, "value", r)) for r in breakdown.reasons)
        alert.matched_alias = "" if matched == alert.target_name else matched[:300]

    ids_to_delete = [alert.id for alert, _ in removed]
    for start in range(0, len(ids_to_delete), 500):
        db.execute(delete(MatchResult).where(MatchResult.id.in_(ids_to_delete[start:start + 500])))
    run.total_matches = len(alerts) - len(removed)
    db.commit()

    record_event(
        db, "RUN_RESCORED", actor="rescore_run.py", entity_ref=run.run_ref,
        payload={
            "case": run.uploaded_filename, "alerts_before": total, "alerts_removed": len(removed),
            "alerts_after": run.total_matches, "kept_resolved": protected, "kept_orphaned": orphaned,
            "threshold": run.threshold, "acronym_min_batch": matching.ACRONYM_MIN_LEN_BATCH,
            "archive_file": archive.name, "archive_sha256": digest,
            "basis": "corrected matching logic; re-scoring can only remove alerts, never add",
        },
    )
    print(f"applied: {total:,} -> {run.total_matches:,} alerts; audit event RUN_RESCORED recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
