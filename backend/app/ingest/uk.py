"""UK — OFSI / FCDO consolidated sanctions list.

The publication carries a one-line "Report Date:" banner above the real header
row, and splits names across ``Name 1``..``Name 6`` (family-name-last ordering,
with ``Name 6`` holding the primary for entities). Both quirks are handled here.

Alias handling differs from the legacy query: OFSI emits one row per alias with
a shared ``OFSI Group ID``/``Unique ID``. The old query de-duplicated those away
entirely. Here they are grouped so alternate spellings remain matchable.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from .base import CanonicalTarget, clean, decode_text, join_unique
from .identity import label_identifier

SOURCE = "UK Sanctions"

_NAME_COLUMNS = ("Name 6", "Name 1", "Name 2", "Name 3", "Name 4", "Name 5")


def _looks_like_header(row: list[str]) -> bool:
    joined = ",".join(cell.strip().lower() for cell in row)
    return "name 1" in joined or "unique id" in joined


def ingest(payload: bytes) -> list[CanonicalTarget]:
    text = decode_text(payload)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    # Skip the "Report Date: dd-Mmm-yyyy" banner (and any other preamble).
    header_index = next(
        (i for i, row in enumerate(rows[:10]) if _looks_like_header(row)), 0
    )
    header = [cell.strip() for cell in rows[header_index]]
    lookup = {name: index for index, name in enumerate(header)}

    def field(row: list[str], name: str) -> str:
        index = lookup.get(name)
        if index is None or index >= len(row):
            return ""
        return clean(row[index])

    def full_name(row: list[str]) -> str:
        parts = [field(row, column) for column in _NAME_COLUMNS]
        assembled = " ".join(part for part in parts if part).strip()
        # Entities and non-Latin designations may have no Latin name at all.
        return assembled or field(row, "Name non-latin script")

    grouped: dict[str, list[list[str]]] = defaultdict(list)
    for row in rows[header_index + 1 :]:
        if not row or not any(cell.strip() for cell in row):
            continue
        key = (
            field(row, "OFSI Group ID")
            or field(row, "Unique ID")
            or field(row, "UN Reference Number")
            or full_name(row)
        )
        if key:
            grouped[key].append(row)

    targets: list[CanonicalTarget] = []
    for group_id, group_rows in grouped.items():
        # "Primary name" rows are the designation proper; the rest are aliases.
        primary_rows = [
            r for r in group_rows if field(r, "Name type").lower().startswith("primary")
        ]
        anchor_rows = primary_rows or group_rows
        anchor = anchor_rows[0]

        primary = full_name(anchor)
        if not primary:
            continue

        aliases = [n for r in group_rows if (n := full_name(r)) and n != primary]

        targets.append(
            CanonicalTarget(
                source=SOURCE,
                watchlist_id=field(anchor, "Unique ID") or group_id,
                target_name=primary,
                entity_type=field(anchor, "Designation Type"),
                sanctions_program=field(anchor, "Regime Name"),
                remarks=field(anchor, "Other Information"),
                # Address country only — nationality is asserted separately
                # below rather than blended into geography.
                geography=join_unique(field(r, "Address Country") for r in group_rows),
                dob=join_unique(field(r, "D.O.B") for r in group_rows),
                aliases=aliases,
                nationality=join_unique(field(r, "Nationality(/ies)") for r in group_rows),
                place_of_birth=join_unique(
                    " ".join(
                        part
                        for part in (field(r, "Town of birth"), field(r, "Country of birth"))
                        if part
                    )
                    for r in group_rows
                ),
                gender=join_unique(field(r, "Gender") for r in group_rows),
                position=join_unique(field(r, "Position") for r in group_rows),
                identifiers=join_unique(
                    (
                        value
                        for r in group_rows
                        for value in (
                            label_identifier(
                                "Passport",
                                field(r, "Passport number"),
                                note=field(r, "Passport additional information"),
                            ),
                            label_identifier(
                                "National identifier",
                                field(r, "National Identifier number"),
                                note=field(r, "National Identifier additional information"),
                            ),
                        )
                    ),
                    separator=" | ",
                ),
                # The OFSI list is the only one of the four that publishes an
                # explicit, quotable justification per designation.
                listing_reason=field(anchor, "UK Statement of Reasons"),
                listed_on=join_unique(
                    field(r, "Date designated") or field(r, "Last Updated") for r in group_rows
                ),
            )
        )
    return targets
