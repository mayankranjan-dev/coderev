"""
Retry queue for reviews that could not be completed.

Rows whose review failed for a transient reason are retried by a background
worker with exponential backoff:

- llm_offline_pending: Ollama was unreachable (connection refused / connect
  timeout).
- llm_timeout: Ollama accepted the request but did not answer in time.
- llm_error: Ollama answered with an HTTP error or a malformed body.

The worker first checks that Ollama is reachable, so attempts are not burned
while it is down. Each attempt increments attempt_count and stores last_error.
After RETRY_MAX_ATTEMPTS attempts the row moves to llm_failed and is left
alone. The queue is the pr_reviews table itself; there is no separate store.

The review and reachability functions are passed in rather than imported, so
this module does not depend on main.py and tests can supply stubs.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import database
from models import PRReview

logger = logging.getLogger("coderev_bot.retry_queue")

RETRYABLE_STATUSES = ("llm_offline_pending", "llm_timeout", "llm_error")
FAILED_STATUS = "llm_failed"

ReviewFn = Callable[[str], Awaitable[dict]]
ReachableFn = Callable[[], Awaitable[bool]]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def max_attempts() -> int:
    return max(1, int(_env_float("RETRY_MAX_ATTEMPTS", 5)))


def backoff_seconds(attempt_count: int) -> float:
    """Delay before the next attempt after `attempt_count` attempts (>= 1)."""
    base = _env_float("RETRY_BACKOFF_BASE_SECONDS", 60)
    cap = _env_float("RETRY_BACKOFF_MAX_SECONDS", 3600)
    return min(cap, base * (2 ** max(0, attempt_count - 1)))


def utcnow() -> datetime:
    # Naive UTC, matching what SQLite hands back for DateTime columns.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def apply_review_result(review: PRReview, result: dict, now: Optional[datetime] = None) -> None:
    """
    Record one review attempt on `review`. Shared by the webhook job, the
    manual endpoint, and the worker so every path counts attempts the same way.
    """
    now = now or utcnow()
    review.attempt_count = (review.attempt_count or 0) + 1
    review.review_text = result.get("review_text")
    review.critical_count = result.get("critical_count", 0)
    review.warning_count = result.get("warning_count", 0)
    review.nitpick_count = result.get("nitpick_count", 0)
    findings = result.get("findings")
    review.findings_json = json.dumps(findings) if findings is not None else None
    review.truncated = bool(result.get("truncated", False))

    status = result.get("status", "llm_error")
    if status in RETRYABLE_STATUSES:
        review.last_error = result.get("error") or status
        if review.attempt_count >= max_attempts():
            review.status = FAILED_STATUS
            review.next_attempt_at = None
        else:
            review.status = status
            review.next_attempt_at = now + timedelta(seconds=backoff_seconds(review.attempt_count))
    else:
        review.status = status
        review.last_error = result.get("error")
        review.next_attempt_at = None


async def run_once(
    review_fn: ReviewFn,
    reachable_fn: ReachableFn,
    now: Optional[datetime] = None,
    batch_size: int = 10,
) -> int:
    """Retry due rows once. Returns how many rows were attempted."""
    if not await reachable_fn():
        return 0

    now = now or utcnow()
    db = database.SessionLocal()
    try:
        due = (
            db.query(PRReview)
            .filter(PRReview.status.in_(RETRYABLE_STATUSES))
            .filter(PRReview.diff_text.isnot(None))
            .filter((PRReview.next_attempt_at.is_(None)) | (PRReview.next_attempt_at <= now))
            .order_by(PRReview.id)
            .limit(batch_size)
            .all()
        )
        for review in due:
            try:
                result = await review_fn(review.diff_text)
            except Exception as exc:  # noqa: BLE001  (worker must keep running)
                logger.exception("Retry of review %s raised.", review.id)
                result = {"status": "llm_error", "error": f"{type(exc).__name__}: {exc}"}
            apply_review_result(review, result, now)
            db.commit()
            logger.info(
                "Retried review %s (attempt %s): %s", review.id, review.attempt_count, review.status
            )
        return len(due)
    finally:
        db.close()


async def worker_loop(review_fn: ReviewFn, reachable_fn: ReachableFn, stop: asyncio.Event) -> None:
    """Poll every RETRY_POLL_SECONDS until `stop` is set. Never raises."""
    interval = max(1.0, _env_float("RETRY_POLL_SECONDS", 30))
    while not stop.is_set():
        try:
            await run_once(review_fn, reachable_fn)
        except Exception:  # noqa: BLE001
            logger.exception("Retry worker iteration failed.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def worker_enabled() -> bool:
    return os.getenv("RETRY_WORKER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
