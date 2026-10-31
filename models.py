"""
SQLAlchemy models for CodeRev Bot.

Columns added after the first release must also be listed in
database.MIGRATION_COLUMNS so existing databases are upgraded in place.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from database import Base

# Unique per (repo, PR, head commit): a GitHub redelivery of the same event
# maps to the same key and is rejected by the database. Manual reviews and
# rows created before head_sha existed have head_sha NULL, and SQLite treats
# NULLs as distinct, so they never collide.
DEDUPE_INDEX_NAME = "uq_pr_reviews_repo_pr_sha"


class PRReview(Base):
    __tablename__ = "pr_reviews"
    __table_args__ = (
        Index(DEDUPE_INDEX_NAME, "repo_name", "pr_number", "head_sha", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    repo_name = Column(String, nullable=False)
    pr_number = Column(Integer, nullable=False)
    pr_title = Column(String, nullable=False)
    pr_url = Column(String, nullable=False)
    head_sha = Column(String, nullable=True)

    diff_text = Column(Text, nullable=True)
    review_text = Column(Text, nullable=True)
    # JSON list of reviewer.findings.Finding dicts. NULL for rows reviewed
    # before structured findings existed; the API re-parses review_text then.
    findings_json = Column(Text, nullable=True)
    # True when the input was cut to fit the model's context window, so the
    # review only covers part of the diff.
    truncated = Column(Boolean, default=False, nullable=False, server_default="0")

    critical_count = Column(Integer, default=0, nullable=False)
    warning_count = Column(Integer, default=0, nullable=False)
    nitpick_count = Column(Integer, default=0, nullable=False)

    # "pending" | "reviewed" | "diff_unavailable"
    # | "llm_offline_pending" (unreachable) | "llm_timeout" | "llm_error"
    # | "llm_failed" (retry attempts exhausted). See retry_queue.py.
    status = Column(String, nullable=False, default="pending")

    # Retry bookkeeping (retry_queue.py). attempt_count counts every review
    # attempt, including the first one made when the row was created.
    attempt_count = Column(Integer, default=0, nullable=False, server_default="0")
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
