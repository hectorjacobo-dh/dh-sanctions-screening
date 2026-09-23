"""Country resolution against what the regulators actually publish.

The geographic control compares the country an officer supplies with the
country on the designation. When a published value fails to resolve, the
control reports "country unknown" and applies no adjustment — which is
indistinguishable from a country that genuinely was not stated. It fails
silently, and it failed for 18.1% of published values.
"""

from __future__ import annotations

import pytest

from app.geography import country_catalogue, resolve_country


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        # ISO alpha-3. The EU publishes these; only alpha-2 was understood, so
        # 1,945 Russian and 553 Iranian designations sat outside the control.
        ("RUS", "RU"),
        ("IRN", "IR"),
        ("PRK", "KP"),
        ("SYR", "SY"),
        ("BLR", "BY"),
        ("COD", "CD"),
        # Comma-inverted forms, as ISO and the UN publish them.
        ("Korea, North", "KP"),
        ("Korea, South", "KR"),
        ("Congo, Democratic Republic of the", "CD"),
        ("Democratic Republic of the Congo", "CD"),
        # Countries that were simply absent from the hand-maintained table.
        ("Rwanda", "RW"),
        ("Uganda", "UG"),
        ("Haiti", "HT"),
        ("Liberia", "LR"),
        ("Maldives", "MV"),
        ("Bosnia and Herzegovina", "BA"),
        ("Marshall Islands", "MH"),
        # Definite article and abbreviated saints.
        ("The Gambia", "GM"),
        ("St Kitts and Nevis", "KN"),
        # Not in ISO 3166-1, but designations are recorded against it.
        ("Kosovo", "XK"),
        # Territories named directly by the regulators.
        ("Region: Gaza", "PS"),
        ("West Bank", "PS"),
        # Local usage the hand table carries and ISO does not.
        ("UAE", "AE"),
        ("KSA", "SA"),
        ("Burma", "MM"),
        # Demonyms — the UK and UN publish nationality, not a country.
        ("Russian", "RU"),
        ("Venezuelan", "VE"),
    ],
)
def test_published_country_values_resolve(published: str, expected: str) -> None:
    assert expected in resolve_country(published), f"{published!r} did not resolve"


def test_the_two_congos_are_not_confused() -> None:
    """A wrong answer is worse than no answer.

    "Congo, Democratic Republic of the" resolved to CG — the *other* Congo —
    because the comma was treated as a separator before the name was matched,
    discarding the half that says which one. The control then compared a DR
    Congo designation against the Republic of the Congo and reported a
    contradiction that does not exist.
    """
    assert resolve_country("Congo, Democratic Republic of the") == frozenset({"CD"})
    assert resolve_country("Congo") == frozenset({"CG"})


def test_a_bare_ambiguous_name_is_not_guessed() -> None:
    """"Korea" alone names two states; picking one would be a guess stated
    as a fact. An empty result is honest — the control treats it as no
    evidence and makes no adjustment."""
    assert resolve_country("Korea") == frozenset()


def test_qualifiers_appended_by_our_own_parser_are_stripped() -> None:
    """The OFAC adapter marks citizenship as "Iran (citizen)"; unstripped,
    the value matched nothing."""
    assert resolve_country("Iran (citizen)") == frozenset({"IR"})
    assert resolve_country("Korea, North (citizen)") == frozenset({"KP"})


def test_genuinely_multi_valued_fields_still_split() -> None:
    """Regulators join several countries into one field; the comma must still
    separate where it really is a separator."""
    assert resolve_country("Russia / Iran") == frozenset({"RU", "IR"})
    assert "IR" in resolve_country("Tehran, Iran")


def test_unknown_stays_unknown() -> None:
    for value in ("", None, "Region: Kafia Kingi", "—"):
        assert resolve_country(value) == frozenset()


def test_the_picker_offers_only_countries_the_matcher_resolves() -> None:
    """The form must not offer a country the engine would then fail on.

    An officer who picks a country from a list is entitled to assume the
    control actually applied to it.
    """
    unresolvable = [
        entry["name"]
        for entry in country_catalogue()
        if entry["code"] not in resolve_country(entry["name"])
    ]
    assert not unresolvable, f"picker offers countries the matcher cannot resolve: {unresolvable[:10]}"


def test_the_picker_uses_full_names_not_codes() -> None:
    catalogue = country_catalogue()
    assert len(catalogue) > 200
    assert all(len(entry["name"]) > 3 for entry in catalogue)
    names = {entry["name"] for entry in catalogue}
    assert "Venezuela" in names and "Germany" in names
