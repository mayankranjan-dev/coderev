"""
SQLAlchemy engine/session setup for CodeRev Bot.

Uses a local SQLite file (audit.db) by default. Override with DATABASE_URL
in the environment if pointing at a different backend.

The engine is built by configure() rather than at import time only, so tests
can point each test at its own temporary SQLite file. SessionLocal is a single
sessionmaker that configure() rebinds, so modules that imported it keep
working after a reconfigure.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

logger = logging.getLogger("coderev_bot.database")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./audit.db")

Base = declarative_base()

SessionLocal = sessionmaker(autocommit=False, autoflush=False)

engine = None


def configure(url: str = DATABASE_URL):
    """(Re)build the engine for `url` and bind SessionLocal to it."""
    global engine
    if engine is not None:
        engine.dispose()
    # check_same_thread=False is required for SQLite when accessed from
    # FastAPI's threaded request handling; it's a no-op for other backends.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    SessionLocal.configure(bind=engine)
    return engine


# Columns added to pr_reviews after the first release: (name, SQL type and
# default). Only ever append. Each is added with ALTER TABLE ADD COLUMN, which
# keeps every existing row and value.
MIGRATION_COLUMNS = [
    ("head_sha", "VARCHAR"),
]


def _backup_sqlite(db_engine) -> Optional[str]:
    """Copy a file-backed SQLite database next to itself before altering it."""
    path = db_engine.url.database
    if db_engine.url.get_backend_name() != "sqlite" or not path or path == ":memory:":
        return None
    if not os.path.exists(path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = f"{path}.bak-{stamp}"
    # sqlite3's backup API gives a consistent copy even if a writer is active.
    src = sqlite3.connect(path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def migrate(db_engine=None) -> list[str]:
    """
    Upgrade an existing pr_reviews table in place. Returns the columns added.

    Additive only: missing columns are added, the dedupe index is created if
    absent, and nothing is dropped or rewritten. A file-backed SQLite database
    is backed up first whenever a column has to be added.
    """
    from models import DEDUPE_INDEX_NAME

    db_engine = db_engine or engine
    inspector = inspect(db_engine)
    if "pr_reviews" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("pr_reviews")}
    missing = [(name, ddl) for name, ddl in MIGRATION_COLUMNS if name not in existing]

    if missing:
        backup_path = _backup_sqlite(db_engine)
        if backup_path:
            logger.info("Backed up database to %s before migrating.", backup_path)

    with db_engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE pr_reviews ADD COLUMN {name} {ddl}"))
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {DEDUPE_INDEX_NAME} "
                "ON pr_reviews (repo_name, pr_number, head_sha)"
            )
        )
    return [name for name, _ in missing]


def init_db() -> None:
    """Create missing tables, then migrate existing ones. Called at startup."""
    import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
    added = migrate(engine)
    if added:
        logger.info("Migrated pr_reviews: added columns %s.", ", ".join(added))


def get_db():
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


configure(DATABASE_URL)
