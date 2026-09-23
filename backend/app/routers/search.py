"""Interactive single-name search.

The batch endpoint answers "screen my whole book". This answers the other
question an officer asks constantly: "is *this one* name a problem?" — typed
straight in, tuned live, answered in milliseconds.

Optional identity fields (date of birth, nationality, national ID) do not
filter results. They are scored as corroborating signals and reported per hit,
because a date-of-birth mismatch is evidence about a *person*, not proof the
name is clean — filtering on them would hide true positives whose identity data
the regulator recorded differently.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..audit import record_event
from ..auth import current_user, require_role
from ..blocking import BlockingIndex
from ..config import get_settings
from ..database import SessionLocal, get_db
from ..geography import resolve_country, resolve_region
from ..ingest.runner import active_snapshot
from ..matching import band_for, score_against_all_names
from ..models import MatchResult, ScreeningRun, User, utcnow
from ..normalize import normalize_name
from ..taxonomy import normalise_brand, normalise_group
from ..screening import _Target, _load_targets

router = APIRouter(prefix="/api/search", tags=["search"])

# The blocking index is expensive to build (~6s over 58k designations) and is
# reused across every interactive query. Rebuilt lazily when the active
# watchlist snapshot changes.
_cache_lock = threading.Lock()
_cached: dict[str, object] = {"targets": None, "index": None, "key": None, "built_at": 0.0}

_YEAR = re.compile(r"(1[89]\d{2}|20\d{2})")


class SearchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    threshold: int = Field(default=75, ge=50, le=100)
    country: str = ""
    dob: str = ""
    nationality: str = ""
    national_id: str = ""
    geo_penalty: int = Field(default=0, ge=0, le=40)
    limit: int = Field(default=50, ge=1, le=200)
    # Empty list means "no restriction" — interactive search defaults to
    # everything, including vessels and aircraft, because a targeted lookup is
    # exactly where an officer may want them.
    subject_types: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    target_name: str
    source: str
    watchlist_id: str
    subject_type: str
    entity_type: str
    sanctions_program: str
    geography: str
    dob: str
    remarks: str
    aliases: str

    # --- the designation as published ---------------------------------------
    nationality: str = ""
    place_of_birth: str = ""
    gender: str = ""
    position: str = ""
    identifiers: str = ""
    # Why the party is listed, in the regulator's own words. The UK publishes a
    # Statement of Reasons, the UN a narrative summary, the EU a remark; OFAC
    # publishes none, so for OFAC this is the role and the linkage instead.
    listing_reason: str = ""
    linked_to: str = ""
    listed_on: str = ""

    score: float
    base_score: float
    geo_adjustment: float
    risk_band: str
    reason_codes: list[str]
    score_breakdown: dict
    # Which published spelling actually matched. "Matched on alias
    # 'PJSC UAC'" is defensible; a score against a different string is not.
    matched_name: str = ""
    dob_signal: str
    nationality_signal: str
    country_signal: str = ""
    id_signal: str


class SearchResponse(BaseModel):
    query: str
    normalized_query: str
    threshold: int
    hits: list[SearchHit]
    total_candidates: int
    elapsed_ms: float
    watchlist_size: int


def _snapshot_key(db: Session) -> str:
    from ..ingest.runner import active_refresh_ids

    return ",".join(str(i) for i in sorted(active_refresh_ids(db)))


def _get_index(db: Session) -> tuple[dict[int, _Target], BlockingIndex]:
    key = _snapshot_key(db)
    with _cache_lock:
        if _cached["key"] == key and _cached["index"] is not None:
            return _cached["targets"], _cached["index"]  # type: ignore[return-value]
        targets, index = _load_targets(db)
        _cached.update({"targets": targets, "index": index, "key": key, "built_at": time.time()})
        return targets, index


def invalidate_index_cache() -> None:
    """Drop the cached index — called after a watchlist refresh."""
    with _cache_lock:
        _cached.update({"targets": None, "index": None, "key": None})


# A signal is the OUTCOME OF A COMPARISON, and a comparison needs two sides.
# These constants exist because the old wording did not say which side was
# missing: a hit whose designation published a date of birth still displayed
# "DOB: not provided" whenever the officer had searched without one, which
# reads as a statement about the sanctions record. An officer who believes the
# regulator holds no date of birth has lost the single strongest discriminator
# between a true match and a namesake — and loses it in the direction of
# clearing the alert.
NOT_SEARCHED = "you searched without this"  # our side is empty
NOT_PUBLISHED = "not published on this designation"  # their side is empty
NOT_COMPARABLE = "present on both, not comparable"


def _dob_signal(query_dob: str, target_dob: str) -> str:
    """Compare dates of birth loosely: regulators publish partial dates."""
    if not query_dob:
        return NOT_SEARCHED
    if not target_dob:
        return NOT_PUBLISHED
    if query_dob.strip() in target_dob:
        return "exact match"
    query_years = set(_YEAR.findall(query_dob))
    target_years = set(_YEAR.findall(target_dob))
    if query_years and target_years:
        if query_years & target_years:
            return "year matches"
        return "year differs"
    return NOT_COMPARABLE


def _country_signal(query_value: str, target_value: str) -> str:
    if not query_value:
        return NOT_SEARCHED
    if not target_value:
        return NOT_PUBLISHED
    query_codes = resolve_country(query_value)
    target_codes = resolve_country(target_value)
    if not query_codes or not target_codes:
        return NOT_COMPARABLE
    return "matches" if query_codes & target_codes else "differs"


def _id_signal(national_id: str, target: _Target) -> str:
    if not national_id:
        return NOT_SEARCHED
    needle = "".join(ch for ch in national_id.upper() if ch.isalnum())
    if not needle:
        return NOT_COMPARABLE
    # Search the structured identifier field first. It used to search only
    # ``remarks``, which for the three non-OFAC sources never held document
    # numbers at all, so a matching passport could not be found there.
    haystack = "".join(
        ch
        for ch in f"{target.identifiers} {target.remarks} {target.watchlist_id}".upper()
        if ch.isalnum()
    )
    if needle in haystack:
        return "found on designation"
    # "Not found" implies a search happened against something. If the
    # designation lists no document at all, say that instead — the two mean
    # very different things to an officer weighing a namesake.
    return "not found" if target.identifiers else NOT_PUBLISHED


@router.post("", response_model=SearchResponse)
def search(
    body: SearchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SearchResponse:
    started = time.perf_counter()
    targets, index = _get_index(db)

    normalized = normalize_name(body.name)
    hits: list[SearchHit] = []
    candidate_ids: set[int] = set()

    allowed = {s.strip() for s in body.subject_types if s.strip()}

    if normalized and targets:
        candidate_ids = index.candidates(normalized)
        for target_id in candidate_ids:
            target = targets.get(target_id)
            if target is None:
                continue
            if allowed and target.subject_type not in allowed:
                continue
            # Score every published spelling, not just the primary. The alias
            # index is what recalled this designation; scoring only the primary
            # threw that away — a search for "UAC" reached "PUBLIC JOINT STOCK
            # COMPANY UNITED AIRCRAFT CORPORATION" through its alias "UNITED
            # AIRCRAFT CORPORATION" and then scored 8.9 against the primary.
            breakdown, matched_name = score_against_all_names(
                body.name,
                target.name,
                target.aliases,
                body.country or body.nationality,
                target.geography,
                geo_penalty=body.geo_penalty,
                # Manual search is recall-first: the officer asked about one
                # name and will read every row.
                recall_mode=True,
            )
            if breakdown.base_score < body.threshold:
                continue

            settings = get_settings()
            hits.append(
                SearchHit(
                    target_name=target.name,
                    source=target.source,
                    watchlist_id=target.watchlist_id,
                    subject_type=target.subject_type,
                    entity_type=target.entity_type,
                    sanctions_program=target.program,
                    geography=target.geography,
                    dob=target.dob,
                    remarks=target.remarks[:600],
                    aliases=(target.aliases or "")[:400],
                    score=round(breakdown.final_score, 2),
                    base_score=round(breakdown.base_score, 2),
                    geo_adjustment=breakdown.geo_adjustment,
                    risk_band=str(
                        band_for(breakdown, body.threshold, settings.strong_match_threshold)
                    ),
                    reason_codes=list(breakdown.reasons),
                    score_breakdown=breakdown.as_dict(),
                    # The designation as published, so the officer adjudicates
                    # against the record rather than against three pills.
                    nationality=target.nationality,
                    place_of_birth=target.place_of_birth,
                    gender=target.gender,
                    position=target.position[:400],
                    identifiers=target.identifiers[:600],
                    listing_reason=target.listing_reason[:2000],
                    linked_to=target.linked_to[:400],
                    listed_on=target.listed_on,
                    matched_name=matched_name,
                    dob_signal=_dob_signal(body.dob, target.dob),
                    # Nationality against nationality. It used to be compared
                    # against ``geography``, which is a registered address —
                    # so an address country could "confirm" a nationality the
                    # list never asserted.
                    nationality_signal=_country_signal(body.nationality, target.nationality),
                    country_signal=_country_signal(body.country, target.geography),
                    id_signal=_id_signal(body.national_id, target),
                )
            )

    hits.sort(key=lambda h: (-h.score, h.target_name))
    hits = hits[: body.limit]

    elapsed = (time.perf_counter() - started) * 1000

    # Searches are part of the compliance record: an officer must be able to
    # show that a name was checked on a given date and what came back.
    record_event(
        db,
        "NAME_SEARCH",
        actor=user.email,
        entity_ref=normalized[:120],
        payload={
            "query": body.name[:200],
            "threshold": body.threshold,
            "country": body.country,
            "dob_provided": bool(body.dob),
            "nationality": body.nationality,
            "id_provided": bool(body.national_id),
            "hit_count": len(hits),
            "top_score": hits[0].score if hits else 0,
            "top_source": hits[0].source if hits else "",
        },
    )

    return SearchResponse(
        query=body.name,
        normalized_query=normalized,
        threshold=body.threshold,
        hits=hits,
        total_candidates=len(candidate_ids),
        elapsed_ms=round(elapsed, 1),
        watchlist_size=len(targets),
    )


@router.post("/warm", status_code=202)
def warm(_: User = Depends(current_user)) -> dict[str, str]:
    """Pre-build the search index so the first query is not slow."""

    def _build() -> None:
        db = SessionLocal()
        try:
            _get_index(db)
        finally:
            db.close()

    threading.Thread(target=_build, daemon=True).start()
    return {"status": "warming"}


# ---------------------------------------------------------------------------
# Promote a search into an adjudicable case
# ---------------------------------------------------------------------------


class CaseRequest(SearchRequest):
    """A search the officer wants to put on the record."""

    subject_reference: str = ""   # internal ref, GRID, ticket number
    note: str = ""                # why this party is being screened
    # Who the counterparty is to us, and which market owns the answer. Both
    # are stamped on the case and copied to every alert so the worklist can be
    # filtered and escalated without anyone guessing.
    counterparty_group: str = ""
    brand: str = ""


@router.post("/case", response_model=dict, status_code=201)
def create_case(
    body: CaseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> dict:
    """Persist a manual search as a case that can be adjudicated.

    A search on its own is ephemeral — it proves nothing and there is nothing to
    attach a document to. Promoting it writes a one-party screening run using
    the *same* tables as a batch upload, so adjudication, reason codes, evidence
    documents, the audit chain and the due-diligence record all apply unchanged.

    A case is created **even when nothing matched**: "screened this party on
    this date against these lists and found no designation" is precisely the
    evidence due diligence exists to produce, and it is worthless if it is not
    recorded.
    """
    settings = get_settings()
    targets, index = _get_index(db)
    if not targets:
        raise HTTPException(
            409, "No active watchlist snapshot. Refresh the watchlists first."
        )

    normalized = normalize_name(body.name)
    if not normalized:
        raise HTTPException(400, "That name contains no screenable characters.")

    query_context = {
        "query": body.name,
        "normalized": normalized,
        "threshold": body.threshold,
        "country": body.country,
        "nationality": body.nationality,
        "dob": body.dob,
        "national_id": body.national_id,
        "subject_types": body.subject_types,
        "subject_reference": body.subject_reference,
        "note": body.note,
    }
    identity = " · ".join(
        part
        for part in (
            f"DOB {body.dob}" if body.dob else "",
            f"nationality {body.nationality}" if body.nationality else "",
            f"country {body.country}" if body.country else "",
            f"ID {body.national_id}" if body.national_id else "",
        )
        if part
    )

    run = ScreeningRun(
        run_ref=str(uuid.uuid4()),
        origin="search",
        uploaded_filename=f"Manual search: {body.name}"[:400],
        uploaded_by=user.email,
        input_sha256=hashlib.sha256(
            json.dumps(query_context, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        threshold=body.threshold,
        strong_threshold=settings.strong_match_threshold,
        geo_penalty=body.geo_penalty,
        subject_types=",".join(body.subject_types) or "Individual,Entity,Unknown",
        counterparty_group=normalise_group(body.counterparty_group),
        brand=normalise_brand(body.brand),
        status="running",
        total_rows=1,
        distinct_names=1,
        input_profile={
            "format": "manual search",
            "header_row": 0,
            "columns": {"entity_name": "typed by operator"},
            "name_column_detection": "manual entry",
            "query": query_context,
            "identity_provided": identity or "none",
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    allowed = {s.strip() for s in body.subject_types if s.strip()}
    region = resolve_region(body.country or body.nationality)
    written = 0

    for target_id in index.candidates(normalized):
        target = targets.get(target_id)
        if target is None:
            continue
        if allowed and target.subject_type not in allowed:
            continue
        # Must use the same scorer as the search above: if the case were
        # scored differently from what was displayed, the saved record would
        # not be the screening the officer actually read.
        breakdown, matched_name = score_against_all_names(
            body.name,
            target.name,
            target.aliases,
            body.country or body.nationality,
            target.geography,
            geo_penalty=body.geo_penalty,
            recall_mode=True,
        )
        if breakdown.base_score < body.threshold:
            continue

        db.add(
            MatchResult(
                run_id=run.id,
                grid=body.subject_reference[:60],
                region=region,
                account_country=(body.country or body.nationality)[:120],
                account_status="",
                created_date="",
                partner_legal_name=body.name,
                ubo_name="",
                tax_id=body.national_id[:120],
                screened_field="Manual",
                target_name=target.name,
                entity_type=target.entity_type[:80],
                subject_type=target.subject_type,
                sanctions_program=target.program,
                remarks=target.remarks,
                source=target.source,
                geography=target.geography,
                dob=target.dob[:200],
                watchlist_id=target.watchlist_id[:120],
                nationality=target.nationality,
                place_of_birth=target.place_of_birth,
                gender=target.gender[:40],
                position=target.position,
                identifiers=target.identifiers,
                listing_reason=target.listing_reason,
                linked_to=target.linked_to,
                listed_on=target.listed_on[:120],
                matched_alias="" if matched_name == target.name else matched_name[:300],
                counterparty_group=normalise_group(body.counterparty_group),
                brand=normalise_brand(body.brand),
                score=breakdown.final_score,
                base_score=breakdown.base_score,
                geo_adjustment=breakdown.geo_adjustment,
                risk_band=str(
                    band_for(breakdown, body.threshold, settings.strong_match_threshold)
                ),
                score_breakdown=breakdown.as_dict(),
                reason_codes=",".join(breakdown.reasons),
                screened_at_utc=utcnow(),
            )
        )
        written += 1

    run.total_matches = written
    run.status = "completed"
    run.progress = 100.0
    run.completed_at = utcnow()
    run.watchlist_snapshot = active_snapshot(db)
    db.commit()

    record_event(
        db,
        "SEARCH_CASE_CREATED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "query": body.name[:200],
            "subject_reference": body.subject_reference,
            "note": body.note[:500],
            "threshold": body.threshold,
            "identity_provided": identity or "none",
            "alerts": written,
            "input_sha256": run.input_sha256,
            "watchlist_snapshot": run.watchlist_snapshot,
        },
    )

    return {
        "run_ref": run.run_ref,
        "alerts": written,
        "cleared_with_no_match": written == 0,
    }
