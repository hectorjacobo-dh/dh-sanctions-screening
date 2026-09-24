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
from .endpoints import ENDPOINTS, LearnedEndpoints, resolve

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


def _last_known_count(db: Session, source: str) -> int:
    """How many records this source produced at its last success.

    The floor a replacement URL has to clear. Without it, a fallback serving
    the wrong file would be accepted purely for being reachable.
    """
    return (
        db.execute(
            select(WatchlistRefresh.record_count)
            .where(WatchlistRefresh.source == source, WatchlistRefresh.status == "success")
            .order_by(WatchlistRefresh.id.desc())
            .limit(1)
        ).scalar()
        or 0
    )


def _load_payload(
    spec: SourceSpec, url: str, db: Session | None = None
) -> tuple[bytes, str, str, list, list]:
    """Obtain the source, recovering from a broken URL where possible.

    Order: the configured URL, then whatever worked last time, then known
    mirrors, then whatever the regulator's own download page currently links
    to. Every candidate must parse with this source's adapter and produce a
    plausible number of records before it is used — see endpoints.py for why
    reachability alone is not enough.

    The operator-supplied local file remains the last resort, so a total
    network outage still cannot block a screening run.
    """
    settings = get_settings()
    endpoint = ENDPOINTS.get(spec.key)

    if endpoint is not None:
        learned = LearnedEndpoints(settings.data_dir / "endpoints.learned.json")
        try:
            resolution = resolve(
                endpoint,
                configured_url=url,
                learned_url=learned.get(spec.key),
                fetch=lambda target: fetch(target).content,
                parse=spec.parser,
                last_known_count=_last_known_count(db, spec.key) if db is not None else 0,
            )
        except Exception as exc:  # noqa: BLE001 - fall through to the local file
            logger.warning("%s: no working URL (%s)", spec.key, exc)
        else:
            learned.remember(spec.key, resolution.url)
            if resolution.recovered:
                logger.warning(
                    "%s: published URL failed; recovered via %s", spec.key, resolution.url
                )
            return (
                resolution.payload,
                resolution.url,
                hashlib.sha256(resolution.payload).hexdigest(),
                resolution.attempts,
                resolution.targets,
            )

    if not spec.local_fallback:
        raise RuntimeError(f"{spec.key}: every source URL failed and no local fallback exists")
    fallback = settings.data_dir / spec.local_fallback
    if not fallback.exists():
        raise RuntimeError(
            f"{spec.key}: every source URL failed. Place a manually downloaded copy at "
            f"{fallback} to keep screening running."
        )
    logger.warning("%s: using the local fallback %s", spec.key, fallback)
    content = fallback.read_bytes()
    return (content, f"file://{fallback.as_posix()}", hashlib.sha256(content).hexdigest(), [], [])


# The fields that make a designation *different* rather than merely present.
# A change to any of them can turn a miss into a hit, so all of them count as
# an amendment. Deliberately excludes nothing an officer would screen against.
_MATERIAL_FIELDS = (
    WatchlistTarget.target_name,
    WatchlistTarget.aliases,
    WatchlistTarget.subject_type,
    WatchlistTarget.sanctions_program,
    WatchlistTarget.geography,
    WatchlistTarget.dob,
    WatchlistTarget.nationality,
    WatchlistTarget.identifiers,
    WatchlistTarget.listing_reason,
)


def _fingerprints(db: Session, refresh_id: int) -> dict[str, int]:
    """Every designation in one snapshot: regulator id -> content hash.

    Keyed on the regulator's own identifier rather than the name, so a party
    that was renamed is reported as amended instead of as one deletion plus one
    unrelated addition.
    """
    rows = db.execute(
        select(WatchlistTarget.watchlist_id, *_MATERIAL_FIELDS).where(
            WatchlistTarget.refresh_id == refresh_id
        )
    ).all()
    out: dict[str, int] = {}
    for row in rows:
        key = (row[0] or "").strip() or f"name:{row[1]}"
        out[key] = hash(row[1:])
    return out


def compute_delta(db: Session, refresh: WatchlistRefresh, previous_id: int | None) -> None:
    """Record what this refresh changed against the snapshot it replaced.

    Stored on the refresh row, once, rather than recomputed on every page load
    — the dashboard is opened far more often than a list changes.
    """
    refresh.compared_to_refresh_id = previous_id
    if previous_id is None:
        refresh.added_count = refresh.removed_count = refresh.amended_count = 0
        return

    before = _fingerprints(db, previous_id)
    after = _fingerprints(db, refresh.id)
    if not before:
        # The predecessor's rows were pruned; a delta would be a fiction.
        refresh.compared_to_refresh_id = None
        return

    before_keys, after_keys = set(before), set(after)
    refresh.added_count = len(after_keys - before_keys)
    refresh.removed_count = len(before_keys - after_keys)
    refresh.amended_count = sum(
        1 for key in before_keys & after_keys if before[key] != after[key]
    )


def refresh_source(db: Session, spec: SourceSpec, *, actor: str = "scheduler") -> WatchlistRefresh:
    """Refresh a single regulator's list. Never raises; records failure state."""
    settings = get_settings()
    url = getattr(settings, spec.url_attr)

    refresh = WatchlistRefresh(source=spec.key, source_url=url, status="running")
    db.add(refresh)
    db.commit()
    db.refresh(refresh)

    try:
        payload, resolved_url, digest, attempts, targets = _load_payload(spec, url, db)
        # Already parsed during validation unless we fell back to a local file.
        if not targets:
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

        # The snapshot this one replaces, captured before it is demoted.
        previous_id = db.execute(
            select(WatchlistRefresh.id).where(
                WatchlistRefresh.source == spec.key,
                WatchlistRefresh.id != refresh.id,
                WatchlistRefresh.is_active.is_(True),
            )
        ).scalar()

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
        db.flush()
        compute_delta(db, refresh, previous_id)
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
                "added": refresh.added_count,
                "removed": refresh.removed_count,
                "amended": refresh.amended_count,
                # A changed data source is exactly what an auditor must be able
                # to see. Recorded only when recovery actually happened, so the
                # chain is not filled with noise on a normal day.
                **(
                    {
                        "url_recovery": [
                            {"url": a.url, "ok": a.ok, "detail": a.detail} for a in attempts
                        ]
                    }
                    if len(attempts) > 1
                    else {}
                ),
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
            # What this authority changed when it last published. None means
            # there was nothing to compare against — a first load, or a
            # predecessor since pruned — which is not the same as "no changes".
            "changes": (
                {
                    "added": active.added_count,
                    "removed": active.removed_count,
                    "amended": active.amended_count,
                }
                if active is not None and active.compared_to_refresh_id is not None
                else None
            ),
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
