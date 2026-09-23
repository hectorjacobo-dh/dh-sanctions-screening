"""Database session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

logger = logging.getLogger(__name__)
settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # WAL keeps the dashboard readable while a long screening run writes.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _ensure_columns() -> None:
    """Add columns that exist on the models but not yet in the database.

    Deliberately not a full migration framework. ``create_all`` creates missing
    *tables* but never alters existing ones, so a new field would otherwise
    force recreating the database — destroying real adjudications and breaking
    the audit chain. Additive ``ALTER TABLE ADD COLUMN`` is safe on SQLite and
    PostgreSQL alike; anything more (renames, type changes) should get Alembic.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                default = ""
                backfill: str | None = None

                if column.default is not None:
                    if column.default.is_scalar:
                        value = column.default.arg
                        literal = f"'{value}'" if isinstance(value, str) else str(value)
                        default = f" DEFAULT {literal}"
                    elif column.default.is_callable:
                        # A callable default (e.g. JSON `dict`) cannot become a
                        # SQL DEFAULT, so existing rows would be left NULL and
                        # then fail response validation. Backfill them instead.
                        try:
                            produced = column.default.arg(None)
                        except Exception:  # noqa: BLE001
                            produced = None
                        if isinstance(produced, dict):
                            backfill = "'{}'"
                        elif isinstance(produced, list):
                            backfill = "'[]'"

                connection.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl}{default}')
                )
                if backfill:
                    connection.execute(
                        text(
                            f'UPDATE {table.name} SET "{column.name}" = {backfill} '
                            f'WHERE "{column.name}" IS NULL'
                        )
                    )
                logger.info(
                    "migrated: added %s.%s%s",
                    table.name,
                    column.name,
                    " (backfilled)" if backfill else "",
                )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
