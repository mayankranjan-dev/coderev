"""
SQLAlchemy engine/session setup for CodeRev Bot.

Uses a local SQLite file (audit.db) by default. Override with DATABASE_URL
in the environment if pointing at a different backend.

The engine is built by configure() rather than at import time only, so tests
can point each test at its own temporary SQLite file. SessionLocal is a single
sessionmaker that configure() rebinds, so modules that imported it keep
working after a reconfigure.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

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


def init_db() -> None:
    """Create tables. Called at app startup, never at import."""
    import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


configure(DATABASE_URL)
