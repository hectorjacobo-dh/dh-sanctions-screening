"""Calibration tests for the pre-processing and scoring controls.

These are the regression tests for the failure modes named in the process
review. Run them after any change to normalisation, blocking or scoring:

    cd backend && pytest -q
"""

from __future__ import annotations

import pytest

from app.blocking import BlockingIndex
from app.geography import countries_contradict, resolve_country, resolve_region
from app.matching import ReasonCode, band_for, score_pair
from app.normalize import acronym_of, normalize_name


# --------------------------------------------------------------------------
# Pre-processing control: legal suffix and punctuation removal
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Trading L.L.C.", "ACME"),
        ("ACME GmbH & Co. KG", "ACME"),
        ("PT Acme Indonesia", "ACME INDONESIA"),
        ("Acme Sdn Bhd", "ACME"),
        ("OOO Akme", "AKME"),
        ("Açmé Café", "ACME"),
        ("ACME, INC.", "ACME"),
        ("Acme Foodstuff Trading LLC", "ACME"),
    ],
)
def test_legal_forms_and_punctuation_are_stripped(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_names_that_are_entirely_legal_form_are_not_emptied() -> None:
    """A partner literally called "Trading LLC" must still be screenable."""
    assert normalize_name("Trading LLC") != ""


# --------------------------------------------------------------------------
# The acronym failure mode called out in the review
# --------------------------------------------------------------------------
def test_acronym_expansion_matches() -> None:
    assert acronym_of("United Aircraft Corporation") == "UAC"
    result = score_pair("UAC", "United Aircraft Corporation")
    assert result.base_score >= 80
    assert ReasonCode.ACRONYM_BRIDGE in result.reasons


def test_acronym_bridge_is_directional_agnostic() -> None:
    forward = score_pair("UAC", "United Aircraft Corporation")
    reverse = score_pair("United Aircraft Corporation", "UAC")
    assert forward.base_score == reverse.base_score


# --------------------------------------------------------------------------
# Fuzzy tolerance
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("partner", "target"),
    [
        ("Acme Trading LLC", "ACME TRADING"),
        ("Mohammed Al-Rashid", "Mohamed Al Rashid"),
        ("Rosneft Oil Company", "ROSNEFT OIL CO"),
        ("Bank Melli Iran", "BANK MELLI"),
    ],
)
def test_near_variants_clear_the_threshold(partner: str, target: str) -> None:
    assert score_pair(partner, target).base_score >= 80


def test_unrelated_names_stay_below_threshold() -> None:
    assert score_pair("Pizza Palace", "Rosneft Oil Company").base_score < 80


# --------------------------------------------------------------------------
# Geographic control — the highest-risk piece of calibration
# --------------------------------------------------------------------------
def test_contradiction_requires_both_sides_known() -> None:
    # OFAC publishes no country: unknown must never count as contradiction.
    assert countries_contradict("Germany", "") is False
    assert countries_contradict("", "Russia") is False
    assert countries_contradict("Germany", "Russia") is True
    assert countries_contradict("Germany", "Germany / Russia") is False


def test_unknown_geography_applies_no_penalty() -> None:
    result = score_pair("Acme Trading", "Acme Trading", "Germany", "")
    assert result.geo_adjustment == 0
    assert ReasonCode.GEO_UNKNOWN in result.reasons


def test_contradiction_deprioritises_but_does_not_suppress() -> None:
    result = score_pair("Acme Trading", "Acme Trading", "Germany", "Russia")
    assert result.geo_adjustment == -15
    assert ReasonCode.GEO_CONTRADICTION in result.reasons
    # Banding uses the pre-geography score, so the alert survives.
    assert band_for(result, 80, 95).value == "Strong Match"


def test_confirmed_geography_is_flagged() -> None:
    result = score_pair("Acme Trading", "Acme Trading", "Russia", "Russian Federation")
    assert ReasonCode.GEO_CONFIRMED in result.reasons


def test_country_resolution_handles_regulator_spellings() -> None:
    assert resolve_country("Russian Federation") == frozenset({"RU"})
    assert resolve_country("Iranian") == frozenset({"IR"})
    assert "AE" in resolve_country("United Arab Emirates / Syria")
    assert "SY" in resolve_country("United Arab Emirates / Syria")


def test_region_mapping() -> None:
    assert resolve_region("United Arab Emirates") == "MENA - talabat"
    assert resolve_region("Singapore") == "APAC"
    assert resolve_region("") == "Unmapped"


# --------------------------------------------------------------------------
# Blocking must not silently lose recall
# --------------------------------------------------------------------------
def test_blocking_recalls_exact_and_fuzzy_variants() -> None:
    index = BlockingIndex()
    index.add(1, "Rosneft Oil Company")
    index.add(2, "United Aircraft Corporation")
    index.add(3, "Pizza Palace")
    index.finalise(3)

    assert 1 in index.candidates(normalize_name("Rosneft Oil Co"))
    assert 2 in index.candidates(normalize_name("UAC"))
    # A typo must still recall via the trigram index.
    assert 1 in index.candidates(normalize_name("Rosnft Oil Company"))


def test_blocking_rejects_queries_before_finalise() -> None:
    index = BlockingIndex()
    index.add(1, "Acme")
    with pytest.raises(RuntimeError):
        index.candidates("ACME")


# --------------------------------------------------------------------------
# Determinism — an audit must be reproducible
# --------------------------------------------------------------------------
def test_scoring_is_deterministic() -> None:
    first = score_pair("Acme Trading LLC", "ACME TRADING", "Germany", "Russia")
    second = score_pair("Acme Trading LLC", "ACME TRADING", "Germany", "Russia")
    assert first.as_dict() == second.as_dict()


def test_acronym_survives_a_legal_prefix() -> None:
    """"PJSC United Aircraft Corporation" must still be reachable as "UAC"."""
    from app.normalize import acronyms_of

    assert "UAC" in acronyms_of("PJSC United Aircraft Corporation")
    assert score_pair("UAC", "PJSC United Aircraft Corporation").base_score >= 80


def test_two_letter_acronyms_do_not_bridge() -> None:
    """"BM" must not match "Bank Melli" — too short to be discriminative."""
    result = score_pair("BM Holding AG", "Bank Melli")
    assert ReasonCode.ACRONYM_BRIDGE not in result.reasons
    assert result.base_score < 80


def test_candidate_cap_keeps_the_strongest_evidence() -> None:
    """A rare token must survive truncation even when a common token floods."""
    index = BlockingIndex(max_candidates=10)
    # 200 decoys sharing only the common token "OIL".
    for i in range(200):
        index.add(i, f"Generic Oil Holding {i}")
    index.add(999, "Rosneft Oil Company")
    index.finalise(201)

    found = index.candidates(normalize_name("Rosneft Oil Co"))
    assert 999 in found, "the rare token 'ROSNEFT' must outrank common-token decoys"


# --------------------------------------------------------------------------
# Resolution: status, risk level and reason
#
# The previous thirteen-code dismissal taxonomy was removed. It made the
# adjudication population analysable in principle, but asked an officer to
# choose among overlapping codes on every alert, and the officers using it said
# it did not make sense to them. A control nobody applies consistently produces
# worse data than a simple one applied correctly.
# --------------------------------------------------------------------------
def test_the_reason_follows_from_the_status() -> None:
    """One judgement, not two. The reason restates the match quality."""
    from app.dismissal import reason_for

    assert reason_for("Positive Match") == "FULL_MATCH"
    assert reason_for("Potential Match") == "PARTIAL_MATCH"
    assert reason_for("False Match") == "NO_MATCH"
    assert reason_for("") == "UNKNOWN"


def test_a_false_match_cannot_be_graded_as_a_risk() -> None:
    """A match that is false carries no risk to grade.

    Allowing "False / Low" would put it in a report beside genuine low-risk
    positives, where it would be counted with them.
    """
    from app.dismissal import validate

    assert validate("False Match", "NO_MATCH", "checked", "Low") is not None
    assert validate("False Match", "NO_MATCH", "checked", "High") is not None
    assert validate("False Match", "NO_MATCH", "checked", "N/A") is None
    assert validate("False Match", "NO_MATCH", "checked", "Unknown") is None


def test_a_live_match_is_graded_high_medium_or_low() -> None:
    from app.dismissal import validate

    for risk in ("High", "Medium", "Low"):
        assert validate("Positive Match", "FULL_MATCH", "confirmed", risk) is None
        assert validate("Potential Match", "PARTIAL_MATCH", "", risk) is None
    assert validate("Positive Match", "FULL_MATCH", "confirmed", "N/A") is not None


def test_confirming_or_clearing_needs_words() -> None:
    """Both commit the business to something and must be defensible.

    Confirming treats a counterparty as designated; clearing removes it from
    the record. A reviewer cannot test either against a blank.
    """
    from app.dismissal import validate

    assert validate("Positive Match", "FULL_MATCH", "", "High") is not None
    assert validate("False Match", "NO_MATCH", "", "N/A") is not None
    assert validate("Positive Match", "FULL_MATCH", "Registry checked", "High") is None
    # "Possible" is explicitly an unfinished judgement, so it does not demand one.
    assert validate("Potential Match", "PARTIAL_MATCH", "", "Medium") is None


def test_a_mismatched_reason_is_rejected() -> None:
    from app.dismissal import validate

    assert validate("Positive Match", "NO_MATCH", "x", "High") is not None
    assert validate("False Match", "FULL_MATCH", "x", "N/A") is not None


def test_unknown_statuses_and_risks_are_rejected() -> None:
    from app.dismissal import validate

    assert validate("Made Up Status", "", "x", "High") is not None
    assert validate("Positive Match", "FULL_MATCH", "x", "Critical") is not None


# --------------------------------------------------------------------------
# Evidence storage
# --------------------------------------------------------------------------
def _local_store(tmp_path):
    """A LocalStore rooted at a temp directory."""
    from app.storage import LocalStore

    return LocalStore(tmp_path)


def test_evidence_is_stored_under_its_digest_and_deduplicated(tmp_path) -> None:
    store = _local_store(tmp_path)
    payload = b"%PDF-1.4 trade licence"

    first = store.save(payload, ".pdf")
    second = store.save(payload, ".pdf")   # same bytes, different alert

    assert first.sha256 == second.sha256
    assert second.deduplicated is True
    assert first.stored_filename.startswith(first.sha256)
    assert store.verify(first.stored_filename, first.sha256) is True
    assert store.read(first.stored_filename) == payload


def test_evidence_rejects_dangerous_and_oversized_uploads() -> None:
    import app.evidence as ev

    assert ev.validate_upload("payload.exe", b"MZ") is not None
    assert ev.validate_upload("script.js", b"alert(1)") is not None
    assert ev.validate_upload("noextension", b"data") is not None
    assert ev.validate_upload("empty.pdf", b"") is not None
    assert ev.validate_upload("big.pdf", b"x" * (ev.MAX_EVIDENCE_BYTES + 1)) is not None
    assert ev.validate_upload("licence.pdf", b"%PDF") is None


def test_evidence_path_traversal_is_refused(tmp_path) -> None:
    """A crafted key must never read a file outside the store."""
    store = _local_store(tmp_path)
    assert store.read("../../../etc/passwd") is None
    assert store.read(r"..\..\windows\system32\config") is None


def test_tampered_evidence_fails_verification(tmp_path) -> None:
    store = _local_store(tmp_path)
    stored = store.save(b"original document", ".pdf")
    (tmp_path / stored.stored_filename).write_bytes(b"substituted document")
    assert store.verify(stored.stored_filename, stored.sha256) is False


def test_gcs_backend_rejects_keys_with_path_structure() -> None:
    """The GCS key check must not be bypassable, even without a live client."""
    from app.storage import GcsStore

    store = GcsStore.__new__(GcsStore)   # no network, no credentials
    store.prefix = "evidence"
    store._bucket = None
    bad_keys = ["../secret", "a/b", "a" + chr(92) + "b", "..", "x/../y"]
    for bad in bad_keys:
        try:
            store._blob(bad)
        except ValueError:
            continue
        raise AssertionError(f"key {bad!r} was not rejected")


def test_storage_backend_selection_is_explicit(monkeypatch) -> None:
    """An unknown backend must fail loudly, not silently fall back to disk."""
    import app.storage as st
    from app.config import get_settings

    st.reset_store_cache()
    settings = get_settings()
    monkeypatch.setattr(settings, "evidence_backend", "s3", raising=False)
    try:
        st.get_store()
    except RuntimeError as exc:
        assert "Unknown EVIDENCE_BACKEND" in str(exc)
    else:
        raise AssertionError("an unknown backend was silently accepted")
    finally:
        st.reset_store_cache()


def test_a_discriminative_name_inside_a_longer_designation_is_found() -> None:
    """OFAC lists "BANCO DE VENEZUELA SA BANCO UNIVERSAL".

    Searching the exact trading name must return it. The length ratio here is
    ~0.5, so the plain length gate alone would suppress a true positive.
    """
    result = score_pair("Banco de Venezuela", "BANCO DE VENEZUELA SA BANCO UNIVERSAL")
    assert result.base_score >= 80
    assert ReasonCode.CONTAINED_NAME in result.reasons


def test_a_short_generic_name_inside_a_longer_one_is_still_suppressed() -> None:
    """The containment rule must not reopen the "Pizza" false-positive hole."""
    result = score_pair("Pizza", "Pizza House Trading Company Limited")
    assert result.base_score < 80
    assert ReasonCode.CONTAINED_NAME not in result.reasons


def test_two_token_short_names_do_not_trigger_containment() -> None:
    result = score_pair("Al Noor", "Al Noor General Trading Establishment Dubai")
    assert ReasonCode.CONTAINED_NAME not in result.reasons


# --------------------------------------------------------------------------
# Timezone: Berlin for humans, UTC for the audit record
# --------------------------------------------------------------------------
def test_local_conversion_handles_summer_and_winter() -> None:
    """Berlin is CEST (+02:00) in summer and CET (+01:00) in winter.

    This is exactly why the audit chain stays on UTC: a local stamp is
    ambiguous across the changeover and shifts twice a year.
    """
    import datetime as dt

    from app.models import to_local

    summer = to_local(dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC))
    winter = to_local(dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC))
    assert summer.utcoffset() == dt.timedelta(hours=2)
    assert winter.utcoffset() == dt.timedelta(hours=1)
    assert summer.hour == 14
    assert winter.hour == 13


def test_naive_timestamps_from_sqlite_are_treated_as_utc() -> None:
    import datetime as dt

    from app.models import to_local

    naive = dt.datetime(2026, 7, 1, 12, 0)          # as SQLite returns it
    aware = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
    assert to_local(naive) == to_local(aware)


def test_audit_canonical_form_is_unaffected_by_display_timezone() -> None:
    """Changing the display timezone must never alter a hash."""
    import datetime as dt

    from app.audit import canonical_payload

    stamp = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
    serialised = canonical_payload(1, stamp, "a", "ACT", "ref", {}, "0" * 64)
    assert "2026-07-01T12:00:00.000000+00:00" in serialised


# --------------------------------------------------------------------------
# Manual search must find a short distinctive name inside long designations
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "target",
    [
        "Gazprom PMC",
        "Gazprombank",
        "PUBLIC JOINT STOCK COMPANY GAZPROM NEFT",
        "GAZPROM BURENIE, OOO",
        "OOO GAZPROMMASH",
    ],
)
def test_search_mode_finds_gazprom_at_ninety(target: str) -> None:
    """"gazprom" returned nothing at 90% — 109 designations contain it.

    Character-level ratios score ~78 because most of the target string is
    unaccounted for. Token alignment scores what matters: the searched name is
    present as a name.
    """
    result = score_pair("gazprom", target, recall_mode=True)
    assert result.base_score >= 90, f"{target}: {result.base_score}"


def test_batch_mode_still_suppresses_short_generic_containment() -> None:
    """The same rule across 719k partner names would be unusable."""
    result = score_pair("Pizza", "Pizza House Trading Company", recall_mode=False)
    assert result.base_score < 80


def test_search_mode_reports_why_it_matched() -> None:
    result = score_pair("gazprom", "Gazprom PMC", recall_mode=True)
    assert ReasonCode.NAME_WITHIN_DESIGNATION in result.reasons
    assert result.token_coverage == 1.0
    assert result.unmatched_target_tokens == 1


def test_alignment_prefers_the_closer_designation() -> None:
    """Fewer unexplained tokens should rank higher."""
    close = score_pair("gazprom", "Gazprom PMC", recall_mode=True).base_score
    far = score_pair(
        "gazprom", "Gazprom Neft Shelf Yamal Trading Holdings", recall_mode=True
    ).base_score
    assert close > far


def test_alignment_ignores_unrelated_names() -> None:
    assert score_pair("gazprom", "Bank Melli Iran", recall_mode=True).base_score < 60
    assert score_pair("Happy Pizza Istanbul", "GAZPROM BURENIE, OOO",
                      recall_mode=True).base_score < 60


def test_token_alignment_counts_each_target_token_once() -> None:
    """A repeated query token must not double-claim one target token."""
    from app.matching import token_alignment

    coverage, unmatched = token_alignment(("BANCO", "BANCO"), ("BANCO", "SUR"))
    assert coverage < 1.0        # only one BANCO available to match
    assert unmatched == 1


def test_reverting_to_unreviewed_needs_no_reason() -> None:
    from app.dismissal import validate

    assert validate("", "", "") is None

def test_switching_to_multi_user_does_not_lock_the_operator_out(tmp_path) -> None:
    """The synthetic single-operator record must not suppress bootstrap.

    It has an unusable password, so if it counted as an existing user the
    switch from single-user to multi-user mode would leave an instance with
    exactly one un-loginable account and no way in.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.auth import UNUSABLE_PASSWORDS, ensure_bootstrap_admin, hash_password
    from app.config import get_settings
    from app.models import Base, User

    engine = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    settings = get_settings()
    was_single = settings.single_user_mode
    settings.single_user_mode = False
    try:
        # Simulate having run in single-operator mode first.
        db.add(User(email="local.operator@deliveryhero.com",
                    hashed_password="!", role="admin"))
        db.commit()

        ensure_bootstrap_admin(db)
        # The return value is the *generated* password, and is None when one was
        # configured explicitly. What matters is that a loginable admin exists.
        loginable = db.execute(
            select(User).where(User.hashed_password.notin_(UNUSABLE_PASSWORDS))
        ).scalars().all()
        assert loginable, "no loginable account was created — operator locked out"
        assert loginable[0].role == "admin"

        # And once a real account exists, bootstrap must not run again.
        db.add(User(email="real@deliveryhero.com",
                    hashed_password=hash_password("a-real-password"), role="admin"))
        db.commit()
        assert ensure_bootstrap_admin(db) is None
        assert "!" in UNUSABLE_PASSWORDS
    finally:
        settings.single_user_mode = was_single
        db.close()


# --------------------------------------------------------------------------
# Network guard: the laptop must not serve strangers when it moves
# --------------------------------------------------------------------------
def test_network_guard_permits_only_configured_ranges() -> None:
    from app.network_guard import NetworkGuardMiddleware

    guard = NetworkGuardMiddleware.__new__(NetworkGuardMiddleware)
    from app.network_guard import parse_networks

    guard.networks = parse_networks("10.0.0.0/8")
    guard.enabled = True

    # The operator, on any network.
    assert guard._permitted("127.0.0.1") is True
    # A colleague on the DH corporate range.
    assert guard._permitted("10.120.87.42") is True
    # A stranger on café or home Wi-Fi — the case this exists for.
    assert guard._permitted("192.168.1.55") is False
    assert guard._permitted("172.31.122.9") is False   # DH *guest* network
    assert guard._permitted("8.8.8.8") is False
    # Unparseable or absent: refuse rather than assume.
    assert guard._permitted(None) is False
    assert guard._permitted("not-an-ip") is False


def test_network_guard_ignores_malformed_config_entries() -> None:
    """A typo in one CIDR must not silently permit everything."""
    from app.network_guard import parse_networks

    nets = parse_networks("10.0.0.0/8, nonsense, 192.168.0.0/16")
    assert len(nets) == 2
    assert str(nets[0]) == "10.0.0.0/8"


# ---------------------------------------------------------------------------
# alias scoring
# ---------------------------------------------------------------------------

# The blocking index recalls a designation by ANY published spelling, but
# scoring used only the primary name — so the spelling that caused the recall
# was discarded before it was ever scored.
#
# This is the requirement the system was commissioned to meet: "UAC" must find
# "United Aircraft Corporation". OFAC lists that party as "PUBLIC JOINT STOCK
# COMPANY UNITED AIRCRAFT CORPORATION", whose initials are PJSCUAC, with the
# real name held as an alias. The search returned nothing.
_UAC_PRIMARY = "PUBLIC JOINT STOCK COMPANY UNITED AIRCRAFT CORPORATION"
_UAC_ALIASES = "UNITED AIRCRAFT CORPORATION / PJSC UAC / PUBLICHNOE AKTSIONERNOE OBSCHESTVO"


def test_acronym_reaches_the_designation_through_its_alias() -> None:
    from app.matching import score_against_all_names

    breakdown, matched = score_against_all_names(
        "UAC", _UAC_PRIMARY, _UAC_ALIASES, recall_mode=True
    )
    assert breakdown.base_score >= 85, (
        f"UAC scored {breakdown.base_score:.1f}; the acronym bridge must reach "
        "the alias 'UNITED AIRCRAFT CORPORATION'"
    )
    assert matched == "UNITED AIRCRAFT CORPORATION"


def test_scoring_the_primary_alone_is_what_failed() -> None:
    """Pins the defect, so a refactor that drops alias scoring fails here."""
    from app.matching import score_against_all_names, score_pair

    primary_only = score_pair("UAC", _UAC_PRIMARY, recall_mode=True)
    assert primary_only.base_score < 50, "precondition: the primary name cannot match UAC"

    with_aliases, _ = score_against_all_names(
        "UAC", _UAC_PRIMARY, _UAC_ALIASES, recall_mode=True
    )
    assert with_aliases.base_score > primary_only.base_score + 40


def test_the_matched_spelling_is_reported() -> None:
    """A score against a string absent from the record is not defensible."""
    from app.matching import score_against_all_names

    _, matched = score_against_all_names(
        "Gazprom Neft", "PUBLIC JOINT STOCK COMPANY GAZPROM NEFT", "GAZPROM NEFT PJSC",
        recall_mode=True,
    )
    assert matched in (
        "PUBLIC JOINT STOCK COMPANY GAZPROM NEFT",
        "GAZPROM NEFT PJSC",
    )


def test_primary_name_wins_when_it_scores_highest() -> None:
    from app.matching import score_against_all_names

    breakdown, matched = score_against_all_names(
        "Rosneft", "ROSNEFT", "SOME UNRELATED TRADING HOUSE", recall_mode=True
    )
    assert matched == "ROSNEFT"
    assert breakdown.base_score == 100


def test_alias_fan_out_is_bounded() -> None:
    """One designation carries 144 aliases; a manual search must stay fast."""
    from app.matching import ALIAS_SCORING_LIMIT, score_against_all_names

    aliases = " / ".join(f"DECOY NAME {i}" for i in range(300)) + " / ROSNEFT OIL COMPANY"
    assert ALIAS_SCORING_LIMIT < 300
    breakdown, matched = score_against_all_names(
        "Rosneft Oil Company", "UNRELATED HOLDING", aliases, recall_mode=True
    )
    # The real spelling sits past the cap, so it is NOT found — asserted rather
    # than hidden, because the cap is a deliberate recall/latency trade-off.
    assert matched != "ROSNEFT OIL COMPANY"


def test_empty_and_missing_aliases_are_safe() -> None:
    from app.matching import score_against_all_names

    for aliases in (None, "", " / ", [], ["", "  "]):
        breakdown, matched = score_against_all_names("Rosneft", "ROSNEFT", aliases)
        assert matched == "ROSNEFT"
        assert breakdown.base_score == 100
