"""Simple schema migration helpers.

Uses a version table to track applied migrations.
Each migration is a SQL string applied in order.
"""

from __future__ import annotations

from sqlmodel import SQLModel, Field, Session as DBSession, select, text, col
from sqlalchemy.engine import Engine

from openclose.log import get_logger

log = get_logger(__name__)


class SchemaMigration(SQLModel, table=True):
    """Tracks applied schema migrations."""

    __tablename__ = "schema_migrations"

    version: int = Field(primary_key=True)
    description: str = ""


# Ordered list of migrations. Each is (version, description, sql).
MIGRATIONS: list[tuple[int, str, str]] = [
    # Version 1 is the initial schema created by SQLModel.metadata.create_all.
    (2, "add plan_in_context to sessions", "ALTER TABLE sessions ADD COLUMN plan_in_context BOOLEAN DEFAULT 0;"),
    (3, "add vision_mode to sessions", "ALTER TABLE sessions ADD COLUMN vision_mode BOOLEAN DEFAULT 0;"),
    # The flag's purpose narrowed from "browser_automation rich mode + Record gate"
    # to "Record gate only" — grounding activation is now config-driven.
    # Requires SQLite ≥3.25 (2018); preserves data and DEFAULT.
    (4, "rename vision_mode to video_compatible", "ALTER TABLE sessions RENAME COLUMN vision_mode TO video_compatible;"),
    (5, "add provider to sessions", "ALTER TABLE sessions ADD COLUMN provider VARCHAR DEFAULT '';"),
]


def stamp_version(engine: Engine) -> None:
    """Mark all migrations as applied (for new databases where create_all
    already created the latest schema)."""
    if not MIGRATIONS:
        return
    max_version = max(v for v, _, _ in MIGRATIONS)
    SchemaMigration.metadata.create_all(engine, tables=[SchemaMigration.__table__])  # type: ignore[attr-defined]
    with DBSession(engine) as session:
        for version, description, _ in MIGRATIONS:
            existing = session.get(SchemaMigration, version)
            if existing is None:
                session.add(SchemaMigration(version=version, description=description))
        session.commit()
    log.info("Stamped migration version to %d (new database)", max_version)


def get_current_version(engine: Engine) -> int:
    """Get the highest applied migration version, or 0 if none."""
    # Ensure migration table exists
    SchemaMigration.metadata.create_all(engine, tables=[SchemaMigration.__table__])  # type: ignore[attr-defined]
    with DBSession(engine) as session:
        result = session.exec(
            select(SchemaMigration.version).order_by(
                col(SchemaMigration.version).desc()
            )
        ).first()
        return result if result is not None else 0


def apply_migrations(engine: Engine) -> int:
    """Apply all pending migrations. Returns number of migrations applied."""
    current = get_current_version(engine)
    applied = 0
    last_version = current

    for version, description, sql in MIGRATIONS:
        if version <= current:
            continue
        log.info("Applying migration %d: %s", version, description)
        with DBSession(engine) as session:
            session.exec(text(sql))  # type: ignore[call-overload]
            session.add(SchemaMigration(version=version, description=description))
            session.commit()
        applied += 1
        last_version = version

    if applied:
        log.info("Applied %d migration(s), now at version %d", applied, last_version)
    return applied
