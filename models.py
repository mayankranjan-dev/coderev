"""
SQLAlchemy models for CodeRev Bot.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from database import Base


class PRReview(Base):
    __tablename__ = "pr_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)

    repo_name = Column(String, nullable=False)
    pr_number = Column(Integer, nullable=False)
    pr_title = Column(String, nullable=False)
    pr_url = Column(String, nullable=False)

    diff_text = Column(Text, nullable=True)
    review_text = Column(Text, nullable=True)

    critical_count = Column(Integer, default=0, nullable=False)
    warning_count = Column(Integer, default=0, nullable=False)
    nitpick_count = Column(Integer, default=0, nullable=False)

    # "pending" | "reviewed" | "llm_offline_pending"
    status = Column(String, nullable=False, default="pending")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
