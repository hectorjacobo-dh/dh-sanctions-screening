"""EU Financial Sanctions Files (FSF) consolidated list.

The EU publishes one semicolon-delimited row per *name/address/identifier
combination*, so a single designated party spans many rows. Rows are grouped on
``Entity_logicalId`` and collapsed, matching the grouping the Compliance team's
existing query performs — but keeping every distinct spelling as an alias
rather than discarding all but one.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from .base import CanonicalTarget, clean, decode_text, join_unique
from .identity import label_identifier

SOURCE = "EU Sanctions"

# The EU has changed column casing between releases; look up case-insensitively
# and accept either the current or the legacy spelling.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "logical_id": ("entity_logicalid", "entity_logical_id"),
    "subject_type": ("entity_subjecttype", "subject_type"),
    "programme": ("entity_programme", "programme"),
    "remark": ("entity_remark",),
    "wholename": ("naal_wholename", "name_alias_wholename"),
    "first_name": ("naal_firstname",),
    "last_name": ("naal_lastname",),
    "function": ("naal_function",),
    "city": ("addr_city",),
    "country": ("addr_countrydescription", "addr_country"),
    "birth_date": ("birt_date",),
    "birth_place": ("birt_place",),
    "birth_country": ("birt_countrydescription", "birt_country"),
    "citizenship": ("citi_countrydescription", "citi_country"),
    "identification": ("iden_number",),
    "identification_country": ("iden_countrydescription", "iden_country"),
    "gender": ("naal_gender",),
    "title": ("naal_title",),
    "publication": ("entity_leba_publication_date", "naal_leba_publication_date"),
    "legal_basis": ("entity_leba_numtitle", "naal_leba_numtitle"),
}


def _build_resolver(header: list[str]):
    # First occurrence wins, not last. The EU header repeats
    # ``Entity_logical_id`` once per sub-record block (columns 1, 9, 23, 35, 44,
    # 52 in the September 2026 file). A dict comprehension keeps the last, which
    # is populated on only 393 of 6,000 rows, so:
    #   * grouping failed for ~93% of rows and every row became its own
    #     "entity" (hence watchlist ids like "row-12513"), which meant EU
    #     aliases never attached to their primary name and were unmatchable;
    #   * the rows carrying birth date, nationality and identification numbers
    #     hold no name at all, so they were dropped by the "no names" guard and
    #     every EU identity field came through empty.
    # Column 1 is the entity-level id and is present on every row.
    lookup: dict[str, int] = {}
    for index, name in enumerate(header):
        lookup.setdefault(name.strip().lower(), index)

    def resolve(row: list[str], key: str) -> str:
        for candidate in _FIELD_ALIASES.get(key, ()):
            index = lookup.get(candidate)
            if index is not None and index < len(row):
                return clean(row[index])
        return ""

    return resolve


def ingest(payload: bytes) -> list[CanonicalTarget]:
    text = decode_text(payload)
    reader = csv.reader(io.StringIO(text), delimiter=";")

    try:
        header = next(reader)
    except StopIteration:
        return []

    resolve = _build_resolver(header)
    grouped: dict[str, list[list[str]]] = defaultdict(list)

    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        logical_id = resolve(row, "logical_id")
        grouped[logical_id or f"row-{len(grouped)}"].append(row)

    targets: list[CanonicalTarget] = []
    for logical_id, rows in grouped.items():
        names: list[str] = []
        for row in rows:
            whole = resolve(row, "wholename")
            if whole:
                names.append(whole)
                continue
            composed = " ".join(
                part for part in (resolve(row, "first_name"), resolve(row, "last_name")) if part
            ).strip()
            if composed:
                names.append(composed)

        if not names:
            continue

        # The longest spelling is the most informative primary; the rest are
        # retained as aliases so alternate transliterations stay matchable.
        primary = max(names, key=len)
        aliases = [n for n in names if n != primary]

        targets.append(
            CanonicalTarget(
                source=SOURCE,
                watchlist_id=logical_id,
                target_name=primary,
                entity_type=join_unique(resolve(r, "subject_type") for r in rows),
                sanctions_program=join_unique(resolve(r, "programme") for r in rows),
                remarks=join_unique(
                    (resolve(r, "remark") for r in rows), separator=" | "
                ),
                # Address country only. Citizenship used to be merged in here,
                # which made a nationality indistinguishable from a registered
                # address and silently strengthened the geographic control on
                # evidence that does not support it.
                geography=join_unique(resolve(r, "country") for r in rows),
                dob=join_unique(resolve(r, "birth_date") for r in rows),
                aliases=aliases,
                nationality=join_unique(resolve(r, "citizenship") for r in rows),
                place_of_birth=join_unique(
                    " ".join(
                        part
                        for part in (resolve(r, "birth_place"), resolve(r, "birth_country"))
                        if part
                    )
                    for r in rows
                ),
                gender=join_unique(resolve(r, "gender") for r in rows),
                position=join_unique(
                    value for r in rows for value in (resolve(r, "title"), resolve(r, "function"))
                ),
                identifiers=join_unique(
                    (
                        label_identifier("Identification", resolve(r, "identification"),
                                         resolve(r, "identification_country"))
                        for r in rows
                    ),
                    separator=" | ",
                ),
                # The EU states its basis as the regulation that designates the
                # party; ``Entity_remark`` carries any narrative it adds.
                listing_reason=join_unique(
                    (resolve(r, "remark") for r in rows), separator=" | "
                ),
                linked_to=join_unique(resolve(r, "legal_basis") for r in rows),
                listed_on=join_unique(resolve(r, "publication") for r in rows),
            )
        )
    return targets
