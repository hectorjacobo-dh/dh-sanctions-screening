"""ORM models.

Audit design note
-----------------
The process review called for "immutable timestamps". A timestamp column alone
proves nothing — anyone with write access can change it. Instead, audit rows
are chained: each record stores the SHA-256 of its own canonical payload
concatenated with the previous record's hash. Altering or deleting any historic
row breaks every subsequent hash, so tampering is *detectable* and the chain can
be re-verified on demand via ``/api/audit/verify``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    """Convert a stored (UTC) timestamp into the configured local timezone.

    Display-only. Nothing persisted and nothing hashed goes through here —
    local time shifts twice a year, so it cannot be the audit representation.
    """
    if value is None:
        return None
    from zoneinfo import ZoneInfo

    from .config import get_settings

    aware = value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        return aware.astimezone(ZoneInfo(get_settings().display_timezone))
    except Exception:  # noqa: BLE001 - bad tz config must not break a report
        return aware


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Re-attach UTC to a timestamp read back from the database.

    SQLite has no native timestamp type and does not persist tzinfo, so values
    written as aware UTC return naive. Comparing one against ``utcnow()`` raises
    "can't subtract offset-naive and offset-aware datetimes". Everything is
    stored as UTC, so a naive value is simply labelled as such rather than
    converted.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200), default="")
    # "admin" may refresh watchlists and manage users; "analyst" may screen and
    # adjudicate; "viewer" is read-only.
    role: Mapped[str] = mapped_column(String(20), default="analyst")
    region: Mapped[str] = mapped_column(String(50), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistRefresh(Base):
    """One ingestion attempt against one regulator. Never updated in place."""

    __tablename__ = "watchlist_refreshes"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    source_url: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|failed
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    # SHA-256 of the raw payload: proves which exact file version was ingested.
    payload_sha256: Mapped[str] = mapped_column(String(64), default="")
    payload_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # What this refresh changed, measured against the snapshot it replaced and
    # stored once rather than recomputed per page load. Counted on the
    # regulator's own identifier, not on the name, so a party that was renamed
    # reads as amended rather than as one deletion and one addition.
    #
    # "Amended" matters on its own: the UN has republished with an unchanged
    # record count and different content — corrections to dates of birth and
    # added aliases. A count alone cannot see that, and an added alias can be
    # the difference between a hit and a miss.
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_count: Mapped[int] = mapped_column(Integer, default=0)
    amended_count: Mapped[int] = mapped_column(Integer, default=0)
    # Null when there was no earlier snapshot to compare with — a first load,
    # or one where the predecessor has been pruned. Distinct from "nothing
    # changed", which is what three zeroes would otherwise imply.
    compared_to_refresh_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WatchlistTarget(Base):
    """A designated party, flattened to the shared canonical schema."""

    __tablename__ = "watchlist_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    refresh_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_refreshes.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(40), index=True)
    watchlist_id: Mapped[str] = mapped_column(String(120), default="")
    target_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), default="")
    # Regulator-agnostic: Individual | Entity | Vessel | Aircraft | Unknown.
    subject_type: Mapped[str] = mapped_column(String(20), default="Unknown", index=True)
    sanctions_program: Mapped[str] = mapped_column(Text, default="")
    remarks: Mapped[str] = mapped_column(Text, default="")
    geography: Mapped[str] = mapped_column(Text, default="")
    dob: Mapped[str] = mapped_column(String(200), default="")
    aliases: Mapped[str] = mapped_column(Text, default="")

    # Structured identity, recovered from what each regulator publishes. Held
    # separately from ``geography`` because a nationality is an assertion about
    # the person and a registered address is not; conflating them removed the
    # officer's ability to see which one the list actually states.
    nationality: Mapped[str] = mapped_column(Text, default="")
    place_of_birth: Mapped[str] = mapped_column(Text, default="")
    gender: Mapped[str] = mapped_column(String(40), default="")
    position: Mapped[str] = mapped_column(Text, default="")
    identifiers: Mapped[str] = mapped_column(Text, default="")

    # Why the party is listed, in the regulator's own words where published.
    listing_reason: Mapped[str] = mapped_column(Text, default="")
    linked_to: Mapped[str] = mapped_column(Text, default="")
    listed_on: Mapped[str] = mapped_column(String(120), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_target_source_name", "source", "normalized_name"),)


class ScreeningRun(Base):
    __tablename__ = "screening_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_ref: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    # "batch" for an uploaded file, "search" for a manual single-name case.
    # Both share this table so adjudication, evidence, the audit chain and the
    # due-diligence record work identically for either.
    origin: Mapped[str] = mapped_column(String(12), default="batch", index=True)
    uploaded_filename: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(320), default="")
    # SHA-256 of the uploaded file: binds the report to the exact input.
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[int] = mapped_column(Integer, default=80)
    strong_threshold: Mapped[int] = mapped_column(Integer, default=95)
    geo_penalty: Mapped[int] = mapped_column(Integer, default=15)
    # Which subject types this run screened, recorded so the report states the
    # scope of the control that was actually applied.
    subject_types: Mapped[str] = mapped_column(
        String(120), default="Individual,Entity,Unknown"
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    distinct_names: Mapped[int] = mapped_column(Integer, default=0)
    total_matches: Mapped[int] = mapped_column(Integer, default=0)
    # How the uploaded file was interpreted: format, header row, and which
    # column was read as the partner name. Shown in the UI so an officer can
    # confirm the engine read the right field before trusting the output.
    # Who the case is about and who owns it. Set once for the whole case and
    # copied onto every alert, so the worklist can be filtered without joining
    # back to the run — and so a report row stays self-describing after the
    # case is gone.
    counterparty_group: Mapped[str] = mapped_column(String(40), default="", index=True)
    brand: Mapped[str] = mapped_column(String(40), default="", index=True)
    input_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    watchlist_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")

    matches: Mapped[list["MatchResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class MatchResult(Base):
    """One partner/target alert. Column names mirror the existing workbook."""

    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("screening_runs.id", ondelete="CASCADE"), index=True
    )

    # --- Partner side -------------------------------------------------
    grid: Mapped[str] = mapped_column(String(60), default="", index=True)
    region: Mapped[str] = mapped_column(String(50), default="")
    account_country: Mapped[str] = mapped_column(String(120), default="")
    account_status: Mapped[str] = mapped_column(String(60), default="")
    created_date: Mapped[str] = mapped_column(String(40), default="")
    partner_legal_name: Mapped[str] = mapped_column(Text, default="")
    ubo_name: Mapped[str] = mapped_column(Text, default="")
    tax_id: Mapped[str] = mapped_column(String(120), default="")
    # Which field produced the hit: "Entity" or "UBO".
    screened_field: Mapped[str] = mapped_column(String(20), default="Entity")

    # --- Watchlist side -----------------------------------------------
    target_name: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[str] = mapped_column(String(80), default="")
    subject_type: Mapped[str] = mapped_column(String(20), default="Unknown", index=True)
    sanctions_program: Mapped[str] = mapped_column(Text, default="")
    remarks: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="", index=True)
    geography: Mapped[str] = mapped_column(Text, default="")
    dob: Mapped[str] = mapped_column(String(200), default="")
    watchlist_id: Mapped[str] = mapped_column(String(120), default="")

    # Copied onto the alert, not joined at read time. The designation the
    # officer adjudicated against must stay legible in the record even after
    # the regulator amends or delists the party.
    nationality: Mapped[str] = mapped_column(Text, default="")
    place_of_birth: Mapped[str] = mapped_column(Text, default="")
    gender: Mapped[str] = mapped_column(String(40), default="")
    position: Mapped[str] = mapped_column(Text, default="")
    identifiers: Mapped[str] = mapped_column(Text, default="")
    listing_reason: Mapped[str] = mapped_column(Text, default="")
    linked_to: Mapped[str] = mapped_column(Text, default="")
    listed_on: Mapped[str] = mapped_column(String(120), default="")
    # The published spelling that actually scored, when it was not the primary
    # name. Empty means the primary matched. Without it the report shows a
    # score against a string the reviewer cannot find on the record.
    matched_alias: Mapped[str] = mapped_column(Text, default="")
    # The officer's risk grading of the resolution. Distinct from ``risk_band``,
    # which is the engine's own assessment of match strength — one is a
    # judgement, the other a measurement, and a report that conflates them
    # cannot be audited.
    risk_level: Mapped[str] = mapped_column(String(20), default="")
    counterparty_group: Mapped[str] = mapped_column(String(40), default="", index=True)
    brand: Mapped[str] = mapped_column(String(40), default="", index=True)

    # --- Engine output -------------------------------------------------
    score: Mapped[float] = mapped_column(Float, default=0.0)
    base_score: Mapped[float] = mapped_column(Float, default=0.0)
    geo_adjustment: Mapped[float] = mapped_column(Float, default=0.0)
    risk_band: Mapped[str] = mapped_column(String(30), default="", index=True)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    reason_codes: Mapped[str] = mapped_column(Text, default="")

    # --- Adjudication (human) -------------------------------------------
    decision: Mapped[str] = mapped_column(String(30), default="")
    # Structured grounds for the decision — see dismissal.py. Required before a
    # match can be cleared or confirmed.
    reason_code: Mapped[str] = mapped_column(String(48), default="", index=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    grc_input: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(320), default="")
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Stamped once, at the moment the engine evaluated this pair.
    screened_at_utc: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    run: Mapped[ScreeningRun] = relationship(back_populates="matches")

    __table_args__ = (Index("ix_match_run_band", "run_id", "risk_band"),)


class EvidenceDocument(Base):
    """A due-diligence document supporting one adjudication.

    Append-only by design: there is no delete endpoint. Evidence that could be
    removed after the fact is not evidence, so a superseded document is marked
    rather than erased.

    The file is stored on disk under its own SHA-256 and the digest is written
    into the audit chain, which proves *this exact document* existed at the
    moment the alert was cleared.
    """

    __tablename__ = "evidence_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("match_results.id", ondelete="CASCADE"), index=True
    )
    run_ref: Mapped[str] = mapped_column(String(36), index=True, default="")
    original_filename: Mapped[str] = mapped_column(Text, default="")
    stored_filename: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(320), default="")
    uploaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)

    # Automatic cross-check of the document against the designation. Advisory
    # only — see doc_crosscheck.py for why this can never decide by itself.
    extraction_status: Mapped[str] = mapped_column(String(20), default="")
    extracted_chars: Mapped[int] = mapped_column(Integer, default=0)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEvent(Base):
    """Append-only, hash-chained event log."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    timestamp_utc: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(320), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_ref: Mapped[str] = mapped_column(String(120), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64), default="")
    record_hash: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (UniqueConstraint("sequence", name="uq_audit_sequence"),)
