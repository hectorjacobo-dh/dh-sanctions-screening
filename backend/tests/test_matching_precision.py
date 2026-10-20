"""False alerts found in a real 83,642-name batch.

A Latin American partner file produced 5,435 alerts at a 95% threshold. After
correcting the logic, 536 remained — 90% of the alerts were wrong, and almost
all of them traced to four faults, each pinned here with the actual pair that
exposed it. The partner names below are invented stand-ins with the same
structure; the designation names are public and exactly as published.
"""

from __future__ import annotations

from app.matching import BATCH_ALIAS_LIMIT, score_against_all_names, score_pair

THRESHOLD = 95


def _batch(partner, target, aliases="", country="Argentina", geography="", kind="Entity"):
    # Exactly as the batch run calls it, including the alias policy.
    breakdown, matched = score_against_all_names(
        partner, target, aliases, country, geography,
        target_subject_type=kind, alias_limit=BATCH_ALIAS_LIMIT,
    )
    return breakdown, matched


# ---------------------------------------------------------------------------
# 1. Acronyms are for organisations, never for people
# ---------------------------------------------------------------------------


def test_initials_of_a_persons_name_are_not_an_abbreviation_of_it() -> None:
    """"AMF SAS" scored 100 against four unrelated individuals because their
    initials happened to spell AMF (Azzam Mansour Fadlallah, Abdelkebir Mohamad
    Fakhiri, Ashtiani Mohammad Fedai ...). A person has no acronym."""
    for person in (
        "AZZAM, Mansour Fadlallah",
        "Abdelkebir Mohamad Fakhiri",
        "ASHTIANI MOHAMMAD FEDAI",
    ):
        breakdown, _ = _batch("AMF SAS", person, kind="Individual")
        assert breakdown.base_score < THRESHOLD, person
        assert breakdown.acronym == 0.0


def test_the_same_initials_still_bridge_to_an_organisation() -> None:
    """The rule is about WHAT is being abbreviated, not about acronyms. A UAC in
    Russia against a Russian designation is exactly the founding requirement."""
    breakdown, _ = _batch(
        "UAC", "United Aircraft Corporation", country="Russia", geography="Russia", kind="Entity"
    )
    assert breakdown.base_score >= 80
    assert breakdown.acronym == 100.0


def test_a_three_letter_acronym_in_batch_needs_the_country_to_agree() -> None:
    """Across 83,642 partner names, 432 of the 461 alerts that rested on the
    acronym bridge alone were three-letter names — each the initials of some
    unrelated designation. Uncorroborated, a three-letter name is not evidence."""
    for country, geography in (("Argentina", "Russia"), ("Argentina", ""), ("", "")):
        breakdown, _ = _batch(
            "UAC", "United Aircraft Corporation", country=country, geography=geography,
            kind="Entity",
        )
        assert breakdown.acronym == 0.0, (country, geography)
        assert breakdown.base_score < THRESHOLD


def test_a_four_letter_acronym_stands_without_corroboration() -> None:
    breakdown, _ = _batch(
        "UACO", "United Aircraft Company Organisation", country="Argentina", kind="Entity"
    )
    assert breakdown.acronym == 100.0


def test_a_designations_acronym_alias_does_not_pair_it_with_a_long_partner_name() -> None:
    """"AGN Alimentos y Bebidas" was paired with the Abdallah Azzam Brigades:
    the designation carries the alias AAB, and A-G-N-A-B... shares its initials.
    In batch only the partner's own name may be the acronym."""
    breakdown, matched = _batch(
        "AGN ALIMENTOS Y BEBIDAS LTDA",
        "Yusuf al-'Uyayri Battalions of the Abdallah Azzam Brigades",
        aliases="AAB / Abdallah Azzam Brigades",
        kind="Entity",
    )
    assert breakdown.base_score < THRESHOLD
    assert breakdown.acronym == 0.0


# ---------------------------------------------------------------------------
# 2. A repeated word is not a distinctive name
# ---------------------------------------------------------------------------


def test_a_repeated_given_name_is_not_a_discriminative_designation() -> None:
    """"ISMAEL ISMAEL" counted as two tokens and thirteen characters, so it
    passed as discriminative and scored 100 against any name containing ISMAEL.
    That is one given name, not a distinctive designation."""
    for partner in ("AGREDO GIRALDO ISMAEL DAVID", "Alan Ismael Herrera"):
        breakdown, _ = _batch(partner, "Ismael Ismael", kind="Individual")
        assert breakdown.base_score < THRESHOLD, partner


# ---------------------------------------------------------------------------
# 3. Similar length is not the same name
# ---------------------------------------------------------------------------


def test_similar_length_does_not_make_a_shared_word_a_match() -> None:
    """token_set_ratio returns 100 whenever one name's SET of words sits inside
    the other's, and the length check only compared raw string lengths — so a
    two-word partner sharing one word with "ISMAEL ISMAEL" scored 100, and a
    one-word core (CAPITAL) scored 100 against three unrelated companies
    because 7 characters against 10 happens to clear 0.70."""
    breakdown, _ = _batch("Capital Fund SA", "T C CAPITAL CO LTD", kind="Entity")
    assert breakdown.base_score < THRESHOLD

    breakdown, _ = _batch("Alvaro Ismael", "Ismael Ismael", kind="Individual")
    assert breakdown.base_score < THRESHOLD


def test_the_same_words_in_a_different_order_still_match() -> None:
    """The gate must not punish an ordinary re-ordering of one name."""
    breakdown, _ = _batch("Hassan Mohammed Ali", "ALI, Mohammed Hassan", kind="Individual")
    assert breakdown.base_score >= THRESHOLD


# ---------------------------------------------------------------------------
# 4. Generic words are not identity, however long
# ---------------------------------------------------------------------------


def test_a_name_made_only_of_generic_words_is_not_discriminative() -> None:
    """"INTERNATIONAL" is thirteen letters, and length alone made it
    "discriminative" — so it paired a partner with Al-Aqsa International
    Foundation, Persia International Bank, Korea International Chemical Joint
    Venture and a dozen more."""
    for designation in (
        "AL-AQSA INTERNATIONAL FOUNDATION",
        "PERSIA INTERNATIONAL BANK",
        "KOREA INTERNATIONAL CHEMICAL JOINT VENTURE",
        "SHEN ZHONG INTERNATIONAL SHIPPING",
    ):
        breakdown, _ = _batch("International SA", designation, kind="Entity")
        assert breakdown.base_score < THRESHOLD, designation


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------


def test_a_genuinely_contained_distinctive_name_is_still_found() -> None:
    """The reason the containment rule exists: a true positive."""
    breakdown, _ = _batch(
        "Banco de Venezuela", "BANCO DE VENEZUELA SA BANCO UNIVERSAL", kind="Entity"
    )
    assert breakdown.base_score >= 80


def test_an_exact_name_still_alerts_even_when_short() -> None:
    breakdown, _ = _batch("ISIS", "ISIS", kind="Entity")
    assert breakdown.base_score == 100.0


def test_batch_does_not_alert_on_an_alias() -> None:
    """The largest remaining source of noise in a real 83,642-name file.

    "JULIA HOLDING E.I.R.L." collapses to JULIA once the legal form and the
    generic word are stripped, and JULIA is a one-word alias of an unrelated
    person: the alert read 100 on names that visibly differ. LIGA GROUP,
    MARTIN LTDA, MLM S.R.L. and LA RIVIERA LTDA did the same."""
    for partner, designation, alias in (
        ("JULIA HOLDING E.I.R.L.", "FERNANDEZ VALENCIA, Guadalupe", "JULIA"),
        ("LIGA GROUP E.I.R.L.", "Expeditionary and Volunteer Corps", "Liga"),
        ("MARTIN LTDA.", "KHAKI, Parviz", "MARTIN"),
        ("MLM S.R.L.", "Katiba Macina van Jama'a Nusrat ul-Islam wa al-Muslimin", "MLM"),
        ("LA RIVIERA LTDA", "GRUPO WISA, S.A.", "LA RIVIERA"),
    ):
        breakdown, matched = _batch(partner, designation, aliases=alias, kind="Entity")
        assert breakdown.base_score < THRESHOLD, partner
        assert matched == designation, "batch must only ever score the primary name"


def test_batch_still_alerts_on_the_primary_name() -> None:
    """Dropping aliases removes alias hits, not the designation's own name."""
    breakdown, matched = _batch(
        "Inversiones Valle S.A.", "INVERSIONES VALLE", aliases="VALLE", kind="Entity"
    )
    assert breakdown.base_score >= THRESHOLD
    assert matched == "INVERSIONES VALLE"


def test_manual_search_still_reaches_a_designation_through_its_alias() -> None:
    """An officer typed one name on purpose and reads every row, so a nickname
    hit is information there. This is what the alias scoring was built for:
    "UAC" reaching OFAC's "PJSC UAC"."""
    breakdown, matched = score_against_all_names(
        "El Comandante", "GUERRA SALINAS, Ismael", "El Comandante",
        recall_mode=True, target_subject_type="Individual",
    )
    assert matched == "El Comandante"
    assert breakdown.base_score >= THRESHOLD


def test_uac_still_alerts_in_batch_through_the_primary_name() -> None:
    """The founding requirement must survive dropping aliases: OFAC's primary is
    the long legal name, and its initials are what a partner called UAC matches.
    A UAC registered in Russia against a Russian designation."""
    breakdown, _ = _batch(
        "UAC",
        "PUBLIC JOINT STOCK COMPANY UNITED AIRCRAFT CORPORATION",
        aliases="PJSC UAC / UNITED AIRCRAFT CORPORATION",
        country="Russia",
        geography="Russia",
        kind="Entity",
    )
    assert breakdown.base_score >= THRESHOLD


def test_manual_search_keeps_its_recall_for_a_short_name_inside_a_designation() -> None:
    """The batch tightening must not leak into manual search, where an officer
    typed one name on purpose and will read every row."""
    result = score_pair("gazprom", "GAZPROM PMC", recall_mode=True)
    assert result.base_score >= 90
