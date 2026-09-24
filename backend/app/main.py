"""Application entrypoint."""

from __future__ import annotations

import datetime as dt
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .audit import record_event
from .auth import ensure_bootstrap_admin
from .config import describe_config_sources, get_settings
from .database import SessionLocal, init_db
from .ingest.runner import active_snapshot, refresh_all
from .network_guard import NetworkGuardMiddleware
from .routers import audit, labels, runs, search, users, watchlists

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
scheduler = BackgroundScheduler(timezone="UTC")


def scheduled_refresh() -> None:
    db = SessionLocal()
    try:
        logger.info("Scheduled watchlist refresh starting")
        refresh_all(db, actor="scheduler")
    finally:
        db.close()
    # A refresh changes the active snapshot, which invalidates the index. Warm
    # it again here, or the next person to search pays the nine-second rebuild.
    _warm_search_index()


def _warm_search_index() -> None:
    """Pre-build the blocking index so the first search is not the slow one.

    Measured cold: 8.9s for the first search, 0.3s for every one after. The
    index is rebuilt from scratch on every process start, so the autostart
    watchdog restarting the service silently handed that 9 seconds to the next
    person who typed a name.
    """
    from .routers.search import _get_index

    db = SessionLocal()
    try:
        started = time.perf_counter()
        targets, _ = _get_index(db)
        logger.info(
            "Search index warmed: %d designations in %.1fs",
            len(targets),
            time.perf_counter() - started,
        )
    except Exception:  # noqa: BLE001 - a warm-up failure must not stop the app
        logger.exception("Search index warm-up failed; it will build on first search")
    finally:
        db.close()


def _lists_are_stale() -> bool:
    """True when any source has never loaded, or is older than the threshold."""
    threshold_hours = settings.refresh_if_stale_hours
    db = SessionLocal()
    try:
        snapshot = active_snapshot(db)
    finally:
        db.close()

    if not snapshot:
        return True

    now = dt.datetime.now(dt.UTC)
    for entry in snapshot.values():
        last_success = entry.get("last_success_utc")
        if not last_success:
            return True
        age_hours = (now - dt.datetime.fromisoformat(last_success)).total_seconds() / 3600
        if age_hours >= threshold_hours:
            return True
    return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()

    db = SessionLocal()
    try:
        generated_password = ensure_bootstrap_admin(db)
        record_event(db, "APPLICATION_STARTED", actor="system", entity_ref="app")
    finally:
        db.close()

    if generated_password:
        # Printed once, never persisted. The administrator must change it.
        logger.warning(
            "\n%s\n  BOOTSTRAP ADMIN CREATED\n  email:    %s\n  password: %s\n"
            "  Change this password immediately after first login.\n%s",
            "=" * 68,
            settings.bootstrap_admin_email,
            generated_password,
            "=" * 68,
        )

    # State the effective security posture every start. The previous failure
    # mode was a config file that was silently not read; this makes the actual
    # applied settings impossible to miss in the log.
    logger.info(
        "config sources: %s", describe_config_sources()
    )
    logger.info(
        "listening on %s:%d | login required: %s | evidence: %s",
        settings.bind_host,
        settings.bind_port,
        "NO (single-user mode)" if settings.single_user_mode else "yes",
        settings.evidence_backend,
    )
    logger.info(
        "network guard: %s | permitted: loopback + %s",
        "on" if settings.network_guard_enabled else "OFF",
        settings.allowed_networks or "(nothing else)",
    )

    # Hard stop, not a warning. Listening on a corporate subnet with no
    # authentication would hand full admin — including export of every alert
    # naming partners with a suspected sanctions nexus — to anyone who can
    # reach the port. There is no configuration in which that is acceptable.
    if settings.bind_host not in ("127.0.0.1", "localhost", "::1") and settings.single_user_mode:
        raise RuntimeError(
            f"Refusing to start: BIND_HOST={settings.bind_host} exposes this service "
            "to the network while SINGLE_USER_MODE=true, which disables login "
            "entirely. Set SINGLE_USER_MODE=false and a strong SECRET_KEY first."
        )

    if settings.secret_key.startswith("CHANGE-ME"):
        logger.warning(
            "SECRET_KEY is the insecure default. Set a strong SECRET_KEY before "
            "exposing this service to anyone other than localhost."
        )

    scheduler.add_job(
        scheduled_refresh,
        CronTrigger(hour=settings.refresh_cron_hour, minute=settings.refresh_cron_minute),
        id="watchlist_refresh",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Watchlist refresh scheduled daily at %02d:%02d UTC",
        settings.refresh_cron_hour,
        settings.refresh_cron_minute,
    )

    # "Open the page and the lists update themselves": refresh in the
    # background at startup, but only when the snapshot is actually stale, so
    # restarting the app repeatedly does not hammer the regulators.
    if settings.refresh_on_startup and _lists_are_stale():
        logger.info(
            "Watchlists are stale (>%dh) — refreshing in the background now",
            settings.refresh_if_stale_hours,
        )
        scheduler.add_job(scheduled_refresh, id="startup_refresh")
    else:
        logger.info("Watchlists are current — no startup refresh needed")

    # Build the search index now, in the background, rather than on the first
    # search. Indexing 33,420 designated parties and their ~96,000 published
    # spellings takes roughly nine seconds, and it was being paid by whoever
    # searched first after a restart — which, with the watchdog restarting the
    # service, was routinely the officer. It looked like the page had hung, so
    # they reloaded, which did nothing except wait for the same build to end.
    scheduler.add_job(_warm_search_index, id="warm_index", misfire_grace_time=600)

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Centralised sanctions screening engine for Delivery Hero group entities. "
        "Ingests OFAC, EU, UK and UN designations, screens partner batches with "
        "calibrated fuzzy matching, and records every action in a hash-chained "
        "audit trail."
    ),
    lifespan=lifespan,
)

# Outermost: decide whether this caller's network may see the service at all,
# before any routing, authentication or page rendering happens.
app.add_middleware(
    NetworkGuardMiddleware,
    allowed=settings.allowed_networks,
    enabled=settings.network_guard_enabled,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(labels.router)
app.include_router(users.router)
app.include_router(watchlists.router)
app.include_router(runs.router)
app.include_router(search.router)
app.include_router(audit.router)


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "single_user_mode": settings.single_user_mode,
    }


# Serve the UI from the API itself. Mounted last so every /api route wins.
# html=True makes "/" resolve to index.html.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
else:  # pragma: no cover - only hit if the static bundle is missing
    logger.warning("UI directory %s not found; API-only mode", STATIC_DIR)
