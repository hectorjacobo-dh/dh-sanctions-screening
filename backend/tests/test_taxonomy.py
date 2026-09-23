"""Counterparty groups and operating brands.

These fields exist so a batch of several thousand names, screened centrally,
can be worked and answered locally. An alert that cannot say whether it is a
rider or a corporate supplier, or which market owns the answer, cannot be
routed — and routing is the first thing that happens when one is escalated.
"""

from __future__ import annotations

from app.taxonomy import BRANDS, COUNTERPARTY_GROUPS, normalise_brand, normalise_group


def test_brands_are_alphabetical() -> None:
    """Ordered for a human reading a dropdown, not by insertion."""
    assert list(BRANDS) == sorted(BRANDS, key=str.casefold)


def test_every_requested_brand_is_present() -> None:
    for brand in (
        "DH Compliance", "Foodpanda", "Foodora", "Yemeksepeti", "PedidosYa",
        "Glovo", "Talabat", "foody", "efood", "Woowa", "HungerStation",
    ):
        assert brand in BRANDS


def test_every_requested_group_is_present() -> None:
    for group in ("Partner", "Supplier", "Employee", "Rider", "Dmart", "Adhoc"):
        assert group in COUNTERPARTY_GROUPS


def test_values_are_accepted_case_insensitively() -> None:
    """An officer typing "foodpanda" means the brand, not a new one."""
    assert normalise_brand("foodpanda") == "Foodpanda"
    assert normalise_brand("  TALABAT ") == "Talabat"
    assert normalise_group("rider") == "Rider"


def test_an_unrecognised_value_becomes_empty_rather_than_a_new_group() -> None:
    """Free text here would splinter into "foodpanda", "FP", "Foodpanda "
    and stop being groupable, which is the only thing the field is for.
    Empty is honest: it means "not stated"."""
    assert normalise_brand("Deliveroo") == ""
    assert normalise_group("Contractor") == ""
    assert normalise_brand(None) == ""
    assert normalise_group("") == ""


def test_the_case_of_the_brand_is_preserved_exactly() -> None:
    """"efood" and "foody" are lower-case brands; title-casing them would
    misname a business in a compliance record."""
    assert "efood" in BRANDS and "eFood" not in BRANDS
    assert "foody" in BRANDS and "Foody" not in BRANDS


def test_unspecified_carries_no_graded_risk() -> None:
    """An unresolved alert has not been assessed, so it has no grade yet.

    Offering High/Medium/Low invited a risk level to be recorded before any
    judgement was made — the opposite of what "Unspecified" means.
    """
    from app.dismissal import risks_for, validate

    assert risks_for("") == ("Unspecified", "N/A")
    assert validate("", "UNKNOWN", "", "High") is not None
    assert validate("", "UNKNOWN", "", "Unspecified") is None
