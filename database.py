"""
SQLAlchemy engine/session setup for CodeRev Bot.

Uses a local SQLite file (audit.db) by default. Override with DATABASE_URL
in the environment if pointing at a different backend.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./audit.db")

# check_same_thread=False is required for SQLite when accessed from
# FastAPI's threaded request handling; it's a no-op for other backends.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
