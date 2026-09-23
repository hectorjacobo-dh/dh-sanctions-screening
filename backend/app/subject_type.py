"""Canonical subject type.

Each regulator encodes "what kind of thing is this?" differently:

    OFAC   "individual" | "-0-" (meaning entity) | "vessel" | "aircraft"
    EU     "P" (person)  | "E" (entity)
    UK     "Individual"  | "Entity" | "Ship"
    UN     individuals and entities live in separate XML blocks

Screening a company name against a person, or against a ship, is a different
question with a different base rate — so the raw codes are folded into one
vocabulary that the UI and reports can filter on.
"""

from __future__ import annotations

INDIVIDUAL = "Individual"
ENTITY = "Entity"
VESSEL = "Vessel"
AIRCRAFT = "Aircraft"
UNKNOWN = "Unknown"

SUBJECT_TYPES = (INDIVIDUAL, ENTITY, VESSEL, AIRCRAFT, UNKNOWN)

_EXACT: dict[str, str] = {
    "p": INDIVIDUAL,
    "e": ENTITY,
    "i": INDIVIDUAL,
    "-0-": ENTITY,
    "": UNKNOWN,
}

_CONTAINS: tuple[tuple[str, str], ...] = (
    # Order matters: the first substring hit wins.
    ("individual", INDIVIDUAL),
    ("person", INDIVIDUAL),
    ("natural", INDIVIDUAL),
    ("vessel", VESSEL),
    ("ship", VESSEL),
    ("aircraft", AIRCRAFT),
    ("plane", AIRCRAFT),
    ("entity", ENTITY),
    ("organisation", ENTITY),
    ("organization", ENTITY),
    ("company", ENTITY),
    ("corporate", ENTITY),
    ("legal", ENTITY),
    ("undertaking", ENTITY),
    ("body", ENTITY),
    ("group", ENTITY),
)


def canonical_subject_type(raw: str | None) -> str:
    """Fold a regulator's subject code into the shared vocabulary."""
    value = (raw or "").strip().lower()
    if value in _EXACT:
        return _EXACT[value]
    for needle, canonical in _CONTAINS:
        if needle in value:
            return canonical
    return UNKNOWN
