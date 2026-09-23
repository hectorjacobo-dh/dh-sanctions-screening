"""Identity recovery from published records.

Both defects covered here were silent: no exception, no log line, just an
officer looking at "DOB: not provided" on a record that states a date of birth.
Silent data loss on the three fields that separate a true match from a namesake
is the most dangerous failure this system can have, because it fails towards
clearing the alert.
"""

from __future__ import annotations

from app.ingest import eu
from app.ingest.identity import (
    audit_clauses,
    label_identifier,
    parse_ofac_remarks,
    split_clauses,
)

# The real published remarks for OFAC SDN entry 24424.
CABELLO = (
    "DOB 15 Apr 1963; citizen Venezuela; Gender Male; "
    "Cedula No. 8370825 (Venezuela); Passport A0237802 (Venezuela)."
)


def test_ofac_remarks_yield_the_full_identity_record() -> None:
    ident = parse_ofac_remarks(CABELLO)
    assert ident.dob == ["15 Apr 1963"]
    assert ident.nationality == ["Venezuela (citizen)"]
    assert ident.gender == "Male"
    assert ident.identifiers == [
        "Cedula No. 8370825 (Venezuela)",
        "Passport A0237802 (Venezuela)",
    ]


def test_labels_ending_in_a_period_are_not_lost() -> None:
    """The regression that hid every Cedula, Tax ID and National ID number.

    The clause pattern ended in ``\\b``. Between the "." of "Cedula No." and
    the following space there is no word boundary, so the whole family of
    ``No.``-suffixed labels failed to match and their numbers were misfiled as
    prose. 21,986 identifier clauses were affected.
    """
    for clause, expected in (
        ("Cedula No. 8370825 (Venezuela)", "Cedula No. 8370825 (Venezuela)"),
        ("National ID No. 216040 (Yemen)", "National ID No. 216040 (Yemen)"),
        ("Tax ID No. 100036386 (Serbia)", "Tax ID No. 100036386 (Serbia)"),
        ("D.N.I. 12345678", "D.N.I. 12345678"),
    ):
        ident = parse_ofac_remarks(clause)
        assert ident.identifiers == [expected], clause
        assert not ident.narrative, f"{clause!r} was misfiled as prose"


def test_the_label_keeps_its_own_punctuation() -> None:
    """A trailing period is stripped from the clause, never from the label."""
    ident = parse_ofac_remarks("Cedula No. 8370825 (Venezuela).")
    assert ident.identifiers == ["Cedula No. 8370825 (Venezuela)"]


def test_citizenship_is_distinguished_from_nationality() -> None:
    """OFAC states both; they are not interchangeable to an adjudicator."""
    assert parse_ofac_remarks("nationality Russia").nationality == ["Russia"]
    assert parse_ofac_remarks("citizen Russia").nationality == ["Russia (citizen)"]


def test_measures_are_not_mistaken_for_identity() -> None:
    ident = parse_ofac_remarks(
        "Secondary sanctions risk: section 1(b) of Executive Order 13224; "
        "Additional Sanctions Information - Subject to Secondary Sanctions"
    )
    assert not ident.identifiers
    assert not ident.dob
    assert len(ident.programme_notes) == 2


def test_linked_to_is_captured_as_linkage() -> None:
    ident = parse_ofac_remarks("Linked To: ISLAMIC REVOLUTIONARY GUARD CORPS (IRGC)-QODS FORCE.")
    assert ident.linked_to == ["ISLAMIC REVOLUTIONARY GUARD CORPS (IRGC)-QODS FORCE"]


def test_aliases_in_remarks_add_recall() -> None:
    ident = parse_ofac_remarks("a.k.a. 'ABU MUHAMMAD'; DOB 1970")
    assert ident.aliases == ["ABU MUHAMMAD"]
    assert ident.dob == ["1970"]


def test_unparsed_clauses_stay_visible_as_residue() -> None:
    """An unrecognised label must surface, not vanish.

    ``audit_clauses`` is the census that keeps the label list honest when a
    regulator introduces a new clause type.
    """
    classified, residue = audit_clauses([CABELLO])
    assert classified["dob"] == 1
    assert classified["identifiers"] == 2
    assert not residue

    _, residue = audit_clauses(["Zorblat 9910"])
    assert residue, "an unknown labelled clause should be reported as residue"


def test_empty_and_malformed_remarks_are_safe() -> None:
    for value in ("", "   ", ";;;", None):  # type: ignore[arg-type]
        ident = parse_ofac_remarks(value)  # type: ignore[arg-type]
        assert ident.dob == []
        assert ident.identifiers == []
    assert split_clauses("") == []


def test_identifier_rendering_is_uniform_across_sources() -> None:
    """One display shape, so an officer reads one format everywhere."""
    assert label_identifier("Passport", "A123", "Venezuela") == "Passport A123 (Venezuela)"
    assert label_identifier("Passport", "A123", "", "expired") == "Passport A123 - expired"
    # No number means no identifier — never a bare label implying one exists.
    assert label_identifier("Passport", "") == ""


# ---------------------------------------------------------------------------
# EU grouping
# ---------------------------------------------------------------------------

# The EU header repeats ``Entity_logical_id`` once per sub-record block. Built
# to the same shape as the published file: the entity id appears twice, and the
# identity row carries no name at all.
_EU_HEADER = (
    "Entity_logical_id;Entity_subject_type;Entity_remark;Naal_wholename;"
    "Naal_gender;Entity_logical_id;Birt_date;Birt_place;Citi_country;Iden_number"
)
_EU_ROWS = (
    # name row: entity id in column 0, nothing in the duplicate column 5
    "101;person;listed by Reg 2025/42;Diosdado CABELLO RONDON;M;;;;;",
    # alias row
    "101;person;listed by Reg 2025/42;CABELLO RONDON Diosdado;M;;;;;",
    # identity row: no name, and the duplicate column is empty here too
    "101;person;listed by Reg 2025/42;;;;1963-04-15;Caracas;Venezuela;V-8370825",
)


def _eu_payload() -> bytes:
    return ("\n".join((_EU_HEADER, *_EU_ROWS)) + "\n").encode("utf-8")


def test_eu_rows_group_on_the_entity_level_id() -> None:
    """Regression: duplicate column names silently broke EU grouping.

    A dict comprehension over the header kept the *last* ``Entity_logical_id``,
    which is populated on only a fraction of rows. Grouping therefore failed
    and every row became its own designation — so EU aliases never attached to
    their primary name, and the rows carrying identity data (which hold no
    name) were dropped by the "no names" guard. Live effect: 31,047 fragments
    instead of 6,234 parties, and 0% EU coverage on date of birth, nationality,
    place of birth and document number.
    """
    targets = eu.ingest(_eu_payload())

    assert len(targets) == 1, "rows for one party must collapse into one designation"
    target = targets[0]
    assert target.watchlist_id == "101", "grouped on the entity id, not a row fallback"

    # The alias survives and stays matchable.
    assert "CABELLO RONDON Diosdado" in target.alias_string()

    # Identity came off the nameless row and still reached the party.
    assert target.dob == "1963-04-15"
    assert target.nationality == "Venezuela"
    assert "Caracas" in target.place_of_birth
    assert "V-8370825" in target.identifiers


def test_eu_nationality_is_not_folded_into_geography() -> None:
    """Geography is a registered address; nationality is an assertion.

    Merging them let an address country corroborate a nationality the list
    never stated, and fed the geographic control evidence it does not have.
    """
    target = eu.ingest(_eu_payload())[0]
    assert target.nationality == "Venezuela"
    assert "Venezuela" not in target.geography
