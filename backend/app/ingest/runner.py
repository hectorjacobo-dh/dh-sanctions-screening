"""Refresh orchestration.

Each source refreshes independently and atomically: the new snapshot is written
first and only promoted to ``is_active`` once it has fully landed. A regulator
being unreachable therefore degrades to "this list is stale" — with the staleness
visible on the dashboard — rather than to "this list is now empty", which would
silently disable a control.

Retention: previous snapshots are kept (default 3) so a historic screening run
can be reproduced against the list as it stood on the day.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..audit import record_event
from ..config import get_settings
from ..models import WatchlistRefresh, WatchlistTarget, as_utc, utcnow
from ..normalize import is_generic_legal_form, normalize_name
from ..subject_type import canonical_subject_type
from . import eu, ofac, uk, un
from .base import CanonicalTarget, fetch

logger = logging.getLogger(__name__)

SNAPSHOTS_RETAINED = 3


@dataclass(frozen=True)
class SourceSpec:
    key: str
    url_attr: str
    parser: Callable[[bytes], list[CanonicalTarget]]
    local_fallback: str = ""


def _ofac_sdn(payload: bytes) -> list[CanonicalTarget]:
    return ofac.ingest_sdn(
        payload,
        add_url="https://www.treasury.gov/ofac/downloads/add.csv",
        alt_url="https://www.treasury.gov/ofac/downloads/alt.csv",
    )


def _ofac_cons(payload: bytes) -> list[CanonicalTarget]:
    return ofac.ingest_consolidated(
        payload,
        add_url="https://www.treasury.gov/ofac/downloads/consolidated/cons_add.csv",
        alt_url="https://www.treasury.gov/ofac/downloads/consolidated/cons_alt.csv",
    )


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("OFAC SDN", "ofac_sdn_url", _ofac_sdn),
    SourceSpec("OFAC Non-SDN", "ofac_cons_url", _ofac_cons),
    SourceSpec("EU Sanctions", "eu_fsf_url", eu.ingest),
    SourceSpec("UK Sanctions", "uk_ofsi_url", uk.ingest, local_fallback="UK-Sanctions-List.csv"),
    SourceSpec("UN Sanctions", "un_consolidated_url", un.ingest, local_fallback="consolidated.xml"),
)

SOURCE_KEYS = tuple(spec.key for spec in SOURCES)


def _load_payload(spec: SourceSpec, url: str) -> tuple[bytes, str, str]:
    """Fetch from the regulator, falling back to an operator-supplied file.

    The fallback exists so a network outage or an endpoint URL change cannot
    block a screening run: an administrator can drop the manually downloaded
    publication into ``data/`` under the documented filename.
    """
    settings = get_settings()
    try:
        result = fetch(url)
        return result.content, result.url, result.sha256
    except Exception as exc:  # noqa: BLE001
        if not spec.local_fallback:
            raise
        fallback = settings.data_dir / spec.local_fallback
        if not fallback.exists():
            raise
        logger.warning(
            "%s: remote fetch failed (%s); using local fallback %s", spec.key, exc, fallback
        )
        content = fallback.read_bytes()
        return content, f"file://{fallback.as_posix()}", hashlib.sha256(content).hexdigest()


def refresh_source(db: Session, spec: SourceSpec, *, actor: str = "scheduler") -> WatchlistRefresh:
    """Refresh a single regulator's list. Never raises; records failure state."""
    settings = get_settings()
    url = getattr(settings, spec.url_attr)

    refresh = WatchlistRefresh(source=spec.key, source_url=url, status="running")
    db.add(refresh)
    db.commit()
    db.refresh(refresh)

    try:
        payload, resolved_url, digest = _load_payload(spec, url)
        targets = spec.parser(payload)

        if not targets:
            raise ValueError("source parsed to zero records — refusing to activate")

        # Drop degenerate rows whose name is nothing but a legal form. These
        # match every company registered under that form and would otherwise
        # dominate the alert queue. Counted and logged, never silently dropped.
        usable = [t for t in targets if not is_generic_legal_form(t.target_name)]
        discarded = len(targets) - len(usable)
        if discarded:
            logger.warning(
                "%s: skipped %d designation(s) whose name is only a legal form",
                spec.key,
                discarded,
            )
        targets = usable
        if not targets:
            raise ValueError("every parsed record was a generic legal form")

        db.bulk_save_objects(
            [
                WatchlistTarget(
                    refresh_id=refresh.id,
                    source=spec.key,
                    watchlist_id=target.watchlist_id[:120],
                    target_name=target.target_name,
                    normalized_name=normalize_name(target.target_name),
                    entity_type=target.entity_type[:80],
                    subject_type=canonical_subject_type(target.entity_type),
                    sanctions_program=target.sanctions_program,
                    remarks=target.remarks,
                    geography=target.geography,
                    dob=target.dob[:200],
                    aliases=target.alias_string(),
                    nationality=target.nationality,
                    place_of_birth=target.place_of_birth,
                    gender=target.gender[:40],
                    position=target.position,
                    identifiers=target.identifiers,
                    listing_reason=target.listing_reason,
                    linked_to=target.linked_to,
                    listed_on=target.listed_on[:120],
                )
                for target in targets
            ]
        )

        # Promote atomically: demote the prior snapshot only now that the new
        # one is committed to the session.
        db.execute(
            update(WatchlistRefresh)
            .where(
                WatchlistRefresh.source == spec.key,
                WatchlistRefresh.id != refresh.id,
                WatchlistRefresh.is_active.is_(True),
            )
            .values(is_active=False)
        )

        refresh.source_url = resolved_url
        refresh.payload_sha256 = digest
        refresh.payload_bytes = len(payload)
        refresh.record_count = len(targets)
        refresh.status = "success"
        refresh.is_active = True
        refresh.completed_at = utcnow()
        db.commit()

        record_event(
            db,
            "WATCHLIST_REFRESH_SUCCESS",
            actor=actor,
            entity_ref=spec.key,
            payload={
                "refresh_id": refresh.id,
                "records": len(targets),
                "skipped_generic_legal_form": discarded,
                "payload_sha256": digest,
                "source_url": resolved_url,
            },
        )
        _prune_old_snapshots(db, spec.key)

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        refresh = db.get(WatchlistRefresh, refresh.id)
        refresh.status = "failed"
        refresh.error = f"{type(exc).__name__}: {exc}"[:2000]
        refresh.completed_at = utcnow()
        db.commit()
        logger.exception("Watchlist refresh failed for %s", spec.key)
        record_event(
            db,
            "WATCHLIST_REFRESH_FAILED",
            actor=actor,
            entity_ref=spec.key,
            payload={"refresh_id": refresh.id, "error": refresh.error},
        )

    return refresh


def _prune_old_snapshots(db: Session, source: str) -> None:
    keep_ids = [
        row[0]
        for row in db.execute(
            select(WatchlistRefresh.id)
            .where(WatchlistRefresh.source == source, WatchlistRefresh.status == "success")
            .order_by(WatchlistRefresh.id.desc())
            .limit(SNAPSHOTS_RETAINED)
        ).all()
    ]
    if not keep_ids:
        return
    stale = db.execute(
        select(WatchlistRefresh.id).where(
            WatchlistRefresh.source == source,
            WatchlistRefresh.status == "success",
            WatchlistRefresh.id.notin_(keep_ids),
        )
    ).scalars().all()
    if stale:
        db.execute(delete(WatchlistTarget).where(WatchlistTarget.refresh_id.in_(stale)))
        db.execute(delete(WatchlistRefresh).where(WatchlistRefresh.id.in_(stale)))
        db.commit()


def refresh_all(db: Session, *, actor: str = "scheduler") -> list[WatchlistRefresh]:
    record_event(db, "WATCHLIST_REFRESH_STARTED", actor=actor, entity_ref="ALL")
    return [refresh_source(db, spec, actor=actor) for spec in SOURCES]


def active_snapshot(db: Session) -> dict[str, dict]:
    """Per-source status for the dashboard."""
    status: dict[str, dict] = {}
    for key in SOURCE_KEYS:
        active = db.execute(
            select(WatchlistRefresh)
            .where(WatchlistRefresh.source == key, WatchlistRefresh.is_active.is_(True))
            .order_by(WatchlistRefresh.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_attempt = db.execute(
            select(WatchlistRefresh)
            .where(WatchlistRefresh.source == key)
            .order_by(WatchlistRefresh.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        # Timestamps are emitted as UTC-aware ISO strings so every consumer can
        # compare them against "now" without re-deriving the timezone.
        completed = as_utc(active.completed_at) if active else None
        started = as_utc(last_attempt.started_at) if last_attempt else None

        status[key] = {
            "source": key,
            "record_count": active.record_count if active else 0,
            "last_success_utc": completed.isoformat() if completed else None,
            "payload_sha256": active.payload_sha256 if active else "",
            "source_url": active.source_url if active else "",
            "last_attempt_status": last_attempt.status if last_attempt else "never",
            "last_attempt_utc": started.isoformat() if started else None,
            "last_error": last_attempt.error if last_attempt and last_attempt.status == "failed" else "",
        }
    return status


def active_refresh_ids(db: Session) -> list[int]:
    return [
        row[0]
        for row in db.execute(
            select(WatchlistRefresh.id).where(WatchlistRefresh.is_active.is_(True))
        ).all()
    ]


def active_target_count(db: Session) -> int:
    ids = active_refresh_ids(db)
    if not ids:
        return 0
    return db.execute(
        select(func.count(WatchlistTarget.id)).where(WatchlistTarget.refresh_id.in_(ids))
    ).scalar_one()


def active_spelling_count(db: Session) -> int:
    """How many published spellings are searchable, across all active snapshots.

    Distinct from ``active_target_count``, which counts designated *parties*.
    The two differ by a factor of roughly three because every regulator
    publishes aliases and transliterations, and all of them are indexed and
    scored.

    Deliberately NOT called by the dashboard endpoint: it scans every active
    row and cost ~590ms on each page load, for a figure the interface does not
    show. Kept for diagnostics and for answering "did we lose designations?".

    When EU grouping was broken
    the table held 58,233 rows, which looked like a larger watchlist but was
    the same 6,234 EU parties counted five or six times over — with each
    fragment isolated, so its spellings could not reinforce each other. The
    party count alone invites "did we lose designations?"; the party count
    beside the spelling count answers it.
    """
    ids = active_refresh_ids(db)
    if not ids:
        return 0
    total = 0
    rows = db.execute(
        select(WatchlistTarget.target_name, WatchlistTarget.aliases).where(
            WatchlistTarget.refresh_id.in_(ids)
        )
    )
    for name, aliases in rows:
        spellings = {(name or "").strip()}
        spellings |= {a.strip() for a in (aliases or "").split(" / ") if a.strip()}
        total += len({s for s in spellings if s})
    return total
