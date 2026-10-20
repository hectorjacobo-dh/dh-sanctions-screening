"""Pydantic request/response models."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str
    full_name: str = ""


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    region: str
    is_active: bool


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, description="Minimum 12 characters")
    full_name: str = ""
    role: str = Field(default="analyst", pattern="^(admin|analyst|viewer)$")
    region: str = ""


class SourceChanges(BaseModel):
    """What one authority changed at its most recent publication."""

    added: int
    removed: int
    amended: int


class WatchlistStatus(BaseModel):
    source: str
    record_count: int
    last_success_utc: str | None
    payload_sha256: str
    source_url: str
    last_attempt_status: str
    last_attempt_utc: str | None
    last_error: str
    # None when there was no earlier snapshot to compare against, which is
    # not the same as a refresh that changed nothing.
    changes: SourceChanges | None = None


class DashboardStatus(BaseModel):
    sources: list[WatchlistStatus]
    total_targets: int
    oldest_successful_refresh_utc: str | None
    any_source_stale: bool
    stale_threshold_hours: int


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_ref: str
    origin: str = "batch"
    uploaded_filename: str
    uploaded_by: str
    input_sha256: str
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    progress: float
    threshold: int
    strong_threshold: int
    score_floor: int = 0
    geo_penalty: int
    subject_types: str
    total_rows: int
    distinct_names: int
    total_matches: int
    counterparty_group: str = ""
    brand: str = ""
    input_profile: dict
    error: str


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    grid: str
    region: str
    account_country: str
    account_status: str
    created_date: str
    partner_legal_name: str
    ubo_name: str
    tax_id: str
    screened_field: str
    target_name: str
    # The published spelling this name was actually scored against, when it was
    # an alias rather than the primary. Empty when the primary name matched.
    matched_alias: str = ""
    # The designation as published: the same record manual search shows, so a
    # case can be dismissed from the alert itself without searching again.
    aliases: str = ""
    nationality: str = ""
    place_of_birth: str = ""
    gender: str = ""
    position: str = ""
    identifiers: str = ""
    listing_reason: str = ""
    linked_to: str = ""
    listed_on: str = ""
    subject_type: str
    entity_type: str
    sanctions_program: str
    remarks: str
    source: str
    geography: str
    dob: str
    watchlist_id: str
    reason_code: str
    evidence_count: int = 0
    score: float
    base_score: float
    geo_adjustment: float
    risk_band: str
    score_breakdown: dict
    reason_codes: str
    decision: str
    # The officer's grading of the resolution, distinct from ``risk_band``
    # above, which is the engine's own assessment of match strength.
    risk_level: str = ""
    counterparty_group: str = ""
    brand: str = ""
    comment: str
    decided_by: str
    decided_at: dt.datetime | None
    screened_at_utc: dt.datetime


class MatchPage(BaseModel):
    items: list[MatchOut]
    total: int
    limit: int
    offset: int


class AdjudicationIn(BaseModel):
    decision: str = Field(pattern="^(Positive Match|Potential Match|False Match|)$")
    reason_code: str = ""
    risk_level: str = ""
    comment: str = ""


class StatusOut(BaseModel):
    """One row of the resolution matrix, as the interface renders it."""

    value: str
    label: str
    reason_code: str
    reason_label: str
    risks: list[str]
    default_risk: str


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    match_id: int
    original_filename: str
    sha256: str
    size_bytes: int
    content_type: str
    note: str
    uploaded_by: str
    uploaded_at: dt.datetime
    superseded: bool
    extraction_status: str = ""
    extracted_chars: int = 0
    # Tolerant of NULL: documents attached before automatic cross-checking
    # existed have no analysis, and a legacy row must not 500 the endpoint.
    analysis: dict | None = None

    @field_validator("analysis", mode="before")
    @classmethod
    def _analysis_never_null(cls, value: object) -> dict:
        return value if isinstance(value, dict) else {}


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    timestamp_utc: dt.datetime
    actor: str
    action: str
    entity_ref: str
    payload: dict
    previous_hash: str
    record_hash: str


class ChainVerification(BaseModel):
    valid: bool
    events_checked: int
    broken_at: int | None
    detail: str
    head_hash: str = ""
