"""SQLite database connection and lifecycle management."""

from __future__ import annotations

import atexit
from pathlib import Path

from sqlalchemy import event, text
from sqlmodel import SQLModel, Session as DBSession, create_engine
from sqlalchemy.engine import Engine

from openclose.config.paths import ConfigPaths
from openclose.log import get_logger

log = get_logger(__name__)


def _configure_sqlite_connection(dbapi_conn: object, _rec: object) -> None:
    """Configure each SQLite connection for reliable persistence."""
    from sqlite3 import Connection as SQLite3Connection

    if isinstance(dbapi_conn, SQLite3Connection):
        cursor = dbapi_conn.cursor()
        # WAL mode allows concurrent reads during writes
        cursor.execute("PRAGMA journal_mode=WAL")
        # FULL synchronous ensures data reaches disk on commit
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()


def init_db(db_path: Path | None = None) -> Engine:
    """Initialize the database and create all tables."""
    if db_path is None:
        db_path = ConfigPaths.db_path()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, echo=False)

    # Apply SQLite pragmas on every new connection
    event.listen(engine, "connect", _configure_sqlite_connection)

    # Check if this is a brand-new database (no tables yet)
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(engine)
    is_new_db = "sessions" not in inspector.get_table_names()

    SQLModel.metadata.create_all(engine)

    # Apply pending schema migrations for existing databases.
    # For new databases, create_all already creates the latest schema,
    # so we just stamp the current version to skip migrations.
    from openclose.storage.migrations import apply_migrations, stamp_version
    if is_new_db:
        stamp_version(engine)
    else:
        apply_migrations(engine)

    log.info("Database initialized at %s", db_path)
    return engine


class Database:
    """High-level database access."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._engine = init_db(db_path)

    @property
    def engine(self) -> Engine:
        return self._engine

    def get_session(self) -> DBSession:
        """Create a new database session."""
        return DBSession(self._engine)

    def close(self) -> None:
        """Checkpoint WAL and dispose the engine. Call on shutdown."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                conn.commit()
            log.info("WAL checkpoint completed")
        except Exception:
            log.warning("WAL checkpoint failed", exc_info=True)
        self._engine.dispose()
        log.info("Database engine disposed")


_db: Database | None = None


def get_db(db_path: Path | None = None) -> Database:
    """Get or create the global Database instance."""
    global _db
    if _db is None:
        _db = Database(db_path)
        atexit.register(_db.close)
    return _db


def close_db() -> None:
    """Explicitly close the global database (for use in shutdown hooks)."""
    global _db
    if _db is not None:
        _db.close()
        _db = None
