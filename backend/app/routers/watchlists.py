"""Watchlist status and refresh endpoints."""

from __future__ import annotations

import datetime as dt
import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record_event
from ..auth import current_user, require_role
from ..database import SessionLocal, get_db
from ..geography import country_catalogue
from ..ingest.runner import (
    SOURCES,
    active_refresh_ids,
    active_snapshot,
    active_target_count,
    refresh_all,
    refresh_source,
)
from ..models import User, WatchlistTarget
from ..schemas import DashboardStatus, WatchlistStatus

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"])

STALE_THRESHOLD_HOURS = 48


def _refresh_all_task(actor: str) -> None:
    db = SessionLocal()
    try:
        refresh_all(db, actor=actor)
    finally:
        db.close()


def _refresh_one_task(source_key: str, actor: str) -> None:
    spec = next((s for s in SOURCES if s.key == source_key), None)
    if spec is None:
        return
    db = SessionLocal()
    try:
        refresh_source(db, spec, actor=actor)
    finally:
        db.close()


@router.get("/status", response_model=DashboardStatus)
def status(db: Session = Depends(get_db), _: User = Depends(current_user)) -> DashboardStatus:
    """Per-source freshness for the dashboard."""
    snapshot = active_snapshot(db)
    sources = [WatchlistStatus(**entry) for entry in snapshot.values()]

    successes = [
        dt.datetime.fromisoformat(s.last_success_utc)
        for s in sources
        if s.last_success_utc
    ]
    oldest = min(successes).isoformat() if successes else None

    now = dt.datetime.now(dt.UTC)
    stale = any(
        s.last_success_utc is None
        or (now - dt.datetime.fromisoformat(s.last_success_utc)).total_seconds()
        > STALE_THRESHOLD_HOURS * 3600
        for s in sources
    )

    return DashboardStatus(
        sources=sources,
        total_targets=active_target_count(db),
        oldest_successful_refresh_utc=oldest,
        any_source_stale=stale,
        stale_threshold_hours=STALE_THRESHOLD_HOURS,
    )


@router.get("/countries")
def countries(_: User = Depends(current_user)) -> list[dict[str, str]]:
    """The country list offered by the search form.

    Full names, from the ISO 3166-1 register. Served from the same module the
    matcher uses, so the interface cannot offer a country the engine would then
    fail to resolve — an officer picking a country from a list is entitled to
    assume the control actually applied.
    """
    return country_catalogue()


@router.get("/snapshot.json")
def download_snapshot(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    """Export the active watchlist as a file the browser workbench can import.

    The shareable workbench cannot refresh itself: the artifact CSP forbids
    outbound requests, and the regulators send no CORS headers, so a browser
    could not fetch them even without the CSP. This endpoint is the bridge —
    this app refreshes from the regulators daily, and hands the result over as
    a file. Public designations only; no partner data of any kind.
    """
    sources = ["OFAC SDN", "OFAC Non-SDN", "EU Sanctions", "UK Sanctions", "UN Sanctions"]
    index = {name: position for position, name in enumerate(sources)}

    def clean(value: str | None, limit: int) -> str:
        return re.sub(r"\s+", " ", value or "").strip()[:limit]

    refresh_ids = active_refresh_ids(db)
    rows = db.execute(
        select(WatchlistTarget).where(WatchlistTarget.refresh_id.in_(refresh_ids))
    ).scalars().all()

    records, seen = [], set()
    for row in rows:
        name = clean(row.target_name, 96)
        key = (name.casefold(), row.source)
        if not name or key in seen:
            continue
        seen.add(key)
        records.append([
            name,
            index.get(row.source, 0),
            (row.subject_type or "Unknown")[0],
            clean(row.sanctions_program, 40),
            clean(row.geography, 26),
            clean(row.watchlist_id, 14),
            clean(row.aliases, 44),
            clean(row.dob, 12),
            # Appended, never reordered: an older workbench reads the first
            # eight positions and ignores the rest, so a new export stays
            # importable into a build that predates these fields.
            clean(row.nationality, 40),
            clean(row.place_of_birth, 48),
            clean(row.identifiers, 120),
            # The grounds for listing are the longest field by far and the one
            # an adjudication has to quote, so it gets the largest budget.
            clean(row.listing_reason, 420),
            clean(row.position, 120),
            clean(row.listed_on, 12),
        ])

    snapshot = active_snapshot(db)
    newest = max(
        (entry["last_success_utc"] for entry in snapshot.values() if entry["last_success_utc"]),
        default=None,
    )
    payload = {
        "src": sources,
        # Self-describing, so an importer can map positions by name instead of
        # hard-coding indices and silently misreading a longer record.
        "fields": [
            "name", "src", "type", "programme", "geography", "watchlist_id",
            "aliases", "dob", "nationality", "place_of_birth", "identifiers",
            "listing_reason", "position", "listed_on",
        ],
        "rec": records,
        "snapshot": (newest or "")[:10] or "unknown",
        "exported_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source_digests": {
            name: entry.get("payload_sha256", "") for name, entry in sorted(snapshot.items())
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    record_event(
        db,
        "WATCHLIST_SNAPSHOT_EXPORTED",
        actor=user.email,
        entity_ref="snapshot",
        payload={
            "designations": len(records),
            "bytes": len(body),
            "newest_source_refresh": newest,
        },
    )

    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="watchlist-snapshot-{stamp}.json"'
        },
    )


@router.post("/refresh", status_code=202)
def refresh(
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
) -> dict[str, str]:
    """Trigger an immediate refresh of all five regulator feeds."""
    record_event(db, "WATCHLIST_REFRESH_REQUESTED", actor=user.email, entity_ref="ALL")
    background.add_task(_refresh_all_task, user.email)
    return {"status": "accepted", "detail": "Refresh started for all sources."}


@router.post("/refresh/{source_key:path}", status_code=202)
def refresh_one(
    source_key: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
) -> dict[str, str]:
    """Refresh a single regulator feed."""
    if not any(s.key == source_key for s in SOURCES):
        raise HTTPException(404, f"Unknown source '{source_key}'")
    record_event(db, "WATCHLIST_REFRESH_REQUESTED", actor=user.email, entity_ref=source_key)
    background.add_task(_refresh_one_task, source_key, user.email)
    return {"status": "accepted", "detail": f"Refresh started for {source_key}."}
