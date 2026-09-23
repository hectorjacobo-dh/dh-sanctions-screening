"""Calibrate the engine against a known answer set.

    python calibrate.py --from-archive "../archive/*/partner-data-archive.json"
    python calibrate.py --from-file "DH Group_Restaurants Matches.xlsx"

Why this exists
---------------
Every other test in this repository checks that the code does what it was
written to do. None of them answers the question that decides whether this may
be relied upon: **does it find what the existing manual process finds?**

A screening engine has two failure directions and they are not symmetrical. A
false positive costs an officer ten minutes. A miss means a designated party
was onboarded and nobody knows. So this measures recall against names the team
has already determined to be matches, and reports the threshold at which each
is found.

Two modes, and only one of them is validation
---------------------------------------------
``--from-file`` is the real thing: the team's own spreadsheet, every name in
it, including the names this engine has never seen. That measures recall
against the manual process.

``--from-archive`` is NOT validation, and the script says so in its output. The
archive holds this engine's own past alerts, so the only names recoverable from
it are the ones the engine already matched — a name it missed produced no alert
and left no trace. Measuring recall on that set asks "of the names it found,
how many does it find?", which is 100% by construction. For the September run:
234 names were screened, 230 appear in alerts, and the 4 that matched nothing
are simply gone.

What ``--from-archive`` *is* good for is a regression check: does today's engine
still find everything the earlier version found, at what score? That is worth
knowing after changes as large as alias scoring and the EU regrouping — it just
is not evidence that the engine agrees with the humans.

Neither mode can measure the false-positive rate. Precision needs the names the
team screened and *cleared*, and no record of those survives.

Privacy
-------
Partner and UBO names are personal data. The printed summary and the committed
report carry counts only. Per-name detail goes to
``data/calibration-detail.csv``, which is gitignored, for the officer's review.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.matching import score_against_all_names  # noqa: E402
from app.normalize import normalize_name  # noqa: E402
from app.routers.search import _get_index  # noqa: E402

# 85 is what the archived run used; the rest show what moving it buys or costs.
THRESHOLDS = (70, 75, 80, 85, 90, 95)


def names_from_archive(path: Path) -> tuple[list[str], dict]:
    """The names the manual process had already flagged as matches."""
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = [
        r
        for r in data["screening_runs"]
        if "restaurants matches" in str(r.get("uploaded_filename", "")).lower()
    ]
    if not runs:
        raise SystemExit("no 'Restaurants Matches' run found in that archive")
    run = runs[0]
    alerts = [m for m in data["match_results"] if m.get("run_id") == run["id"]]

    names = sorted(
        {
            (m.get("partner_legal_name") or "").strip()
            for m in alerts
            if (m.get("partner_legal_name") or "").strip()
        }
    )
    return names, {
        "source": run.get("uploaded_filename"),
        "threshold": run.get("threshold"),
        "rows": run.get("total_rows"),
        "alerts_then": run.get("total_matches"),
        "names_recovered": len(names),
    }


def names_from_file(path: Path) -> tuple[list[str], dict]:
    """Read an answer set straight from the team's own spreadsheet."""
    from app.screening import _find_header, _iter_raw_rows, _resolve_columns

    rows, fmt = _iter_raw_rows(path.read_bytes(), path.name)
    materialised = list(rows)
    header_row = _find_header(materialised)
    columns = _resolve_columns(materialised[header_row])
    position = columns.get("entity_name")
    if position is None:
        raise SystemExit(f"no name column detected in {path.name}")

    names = sorted(
        {
            row[position].strip()
            for row in materialised[header_row + 1 :]
            if len(row) > position and row[position].strip()
        }
    )
    return names, {"source": path.name, "format": fmt, "names_recovered": len(names)}


def best_score(name: str, targets: dict, index) -> tuple[float, str, str]:
    """The engine's strongest match for one name, as the search would find it."""
    normalized = normalize_name(name)
    best, designation, matched_on = 0.0, "", ""
    for target_id in index.candidates(normalized):
        target = targets.get(target_id)
        if target is None:
            continue
        breakdown, spelling = score_against_all_names(
            name, target.name, target.aliases, None, target.geography, recall_mode=True
        )
        if breakdown.base_score > best:
            best, designation, matched_on = breakdown.base_score, target.name, spelling
    return best, designation, matched_on


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-archive")
    parser.add_argument("--from-file")
    parser.add_argument("--report", default="../deploy/CALIBRATION.md")
    parser.add_argument(
        "--mode-note", default="", help="override the banner describing the mode"
    )
    args = parser.parse_args()

    if args.from_archive:
        found_paths = sorted(glob.glob(args.from_archive))
        if not found_paths:
            raise SystemExit(f"no archive matched {args.from_archive}")
        names, baseline = names_from_archive(Path(found_paths[-1]))
        baseline["mode"] = "regression"
        for line in (
            "",
            "!! REGRESSION CHECK, NOT VALIDATION",
            "   This answer set came from the engine's own past alerts, so it contains",
            "   only names the engine already matched. Recall here is 100% by",
            "   construction and says nothing about agreement with the manual process.",
            "   For real calibration, pass --from-file with the team's spreadsheet.",
            "",
        ):
            print(line)
    elif args.from_file:
        names, baseline = names_from_file(Path(args.from_file))
        baseline["mode"] = "validation"
    else:
        raise SystemExit("pass --from-archive or --from-file")

    print(f"answer set: {len(names)} names from {baseline['source']}")

    db = SessionLocal()
    targets, index = _get_index(db)
    print(f"screening against {len(targets):,} designated parties\n")

    detail = []
    for name in names:
        score, designation, matched_on = best_score(name, targets, index)
        detail.append(
            {
                "name": name,
                "best_score": round(score, 1),
                "best_designation": designation,
                "matched_on": matched_on,
            }
        )

    hits = {t: sum(1 for d in detail if d["best_score"] >= t) for t in THRESHOLDS}
    misses = [d for d in detail if d["best_score"] < min(THRESHOLDS)]

    print(f"{'threshold':>10}  {'found':>6}  {'missed':>7}  recall")
    for threshold in THRESHOLDS:
        print(
            f"{threshold:>9}%  {hits[threshold]:>6}  {len(names) - hits[threshold]:>7}"
            f"  {hits[threshold] / len(names) * 100:5.1f}%"
        )

    bands: Counter = Counter()
    for item in detail:
        score = item["best_score"]
        bands["no candidate" if not score else "100" if score >= 100 else f"{int(score // 10) * 10}s"] += 1
    print("\nbest-score distribution:")
    for key in sorted(bands, key=str):
        print(f"   {key:>13}: {bands[key]}")

    # Per-name detail stays local: these are partner and UBO names.
    detail_path = Path("../data/calibration-detail.csv")
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    with detail_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(detail[0]))
        writer.writeheader()
        writer.writerows(sorted(detail, key=lambda d: d["best_score"]))
    print(f"\nper-name detail (local only, gitignored): {detail_path.resolve()}")
    if misses:
        print(f"  ^ begin with the {len(misses)} name(s) scoring below {min(THRESHOLDS)}%")

    report = Path(args.report)
    report.write_text(_render(names, baseline, hits, bands, misses), encoding="utf-8")
    print(f"summary (counts only, safe to share): {report.resolve()}")
    return 0


def _render(names, baseline, hits, bands, misses) -> str:
    total = len(names)
    regression = baseline.get("mode") == "regression"
    lines = [
        "# Calibration against the manual process"
        if not regression
        else "# Regression check — NOT a calibration",
        "",
        "Counts only — no partner or UBO names. Per-name detail is written to",
        "`data/calibration-detail.csv`, which is gitignored.",
        "",
    ]
    if regression:
        lines += [
            "> **This is not validation, and must not be cited as it.**",
            ">",
            "> The answer set was recovered from the engine's own archived alerts, so it",
            "> contains only names the engine had already matched. A name it missed",
            "> produced no alert and left no trace — of the 234 names screened in",
            "> September, 230 appear in alerts and the 4 that matched nothing are gone.",
            "> Recall below is therefore 100% by construction.",
            ">",
            "> What it does show: after alias scoring, the EU regrouping and the identity",
            "> work, today's engine still finds everything the earlier version found.",
            "> That is a regression check, not agreement with the humans.",
            ">",
            "> Real calibration needs the team's own file:",
            "> `python calibrate.py --from-file \"DH Group_Restaurants Matches in Sanctions Lists.xlsx\"`",
            "",
        ]
    lines += [
        "## Answer set",
        "",
        f"- Source: `{baseline['source']}`",
        f"- Names recovered: **{baseline.get('names_recovered')}**",
    ]
    if baseline.get("alerts_then"):
        lines += [
            f"- Rows in the original file: {baseline.get('rows')}",
            f"- Alerts raised at the time, at {baseline.get('threshold')}%: "
            f"{baseline.get('alerts_then')}",
        ]
    lines += [
        "",
        "Precision is not measurable in either mode: that needs the names the team",
        "screened and cleared, and no record of those survives.",
        "",
        "## Recall by threshold",
        "",
        "| Threshold | Found | Missed | Recall |",
        "|---|---|---|---|",
    ]
    for threshold, count in hits.items():
        lines.append(f"| {threshold}% | {count} | {total - count} | {count / total * 100:.1f}% |")
    lines += [
        "",
        "## Best-score distribution",
        "",
        "| Band | Names |",
        "|---|---|",
        *(f"| {key} | {value} |" for key, value in sorted(bands.items(), key=str)),
        "",
        "## How to read this",
        "",
        "A miss is not symmetrical with a false positive. A false positive costs an",
        "officer ten minutes; a miss means a designated party was onboarded and",
        "nobody knows. Every name below the operating threshold should be examined",
        "individually before this engine is relied upon as the control of record.",
        "",
        f"Names scoring below {min(hits)}%: **{len(misses)}**.",
        "",
        "A low score does not automatically mean the engine is wrong. The manual",
        "process produced false positives of its own, so a name it flagged may",
        "simply not be a designated party. Each one has to be read.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
