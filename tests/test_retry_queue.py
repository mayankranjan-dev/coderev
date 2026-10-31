import asyncio
import functools
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest

import database
import main
import retry_queue
from models import PRReview

NOW = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def _retry_env(monkeypatch):
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RETRY_BACKOFF_BASE_SECONDS", "60")
    monkeypatch.setenv("RETRY_BACKOFF_MAX_SECONDS", "3600")


class FakeOllama:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.reachable = True

    async def review(self, code):
        self.calls += 1
        return dict(self.results.pop(0))

    async def check(self):
        return self.reachable


OK = {"review_text": "NITPICK: a.py:1 x Correction: y", "critical_count": 0,
      "warning_count": 0, "nitpick_count": 1, "status": "reviewed", "error": None}
TIMEOUT = {"review_text": None, "status": "llm_timeout", "error": "timed out after 10s"}
OFFLINE = {"review_text": None, "status": "llm_offline_pending", "error": "unreachable (ConnectError)"}


def add_row(session, status="llm_offline_pending", attempts=1, next_at=None, diff="+x = 1"):
    row = PRReview(repo_name="o/r", pr_number=1, pr_title="t", pr_url="u", diff_text=diff,
                   status=status, attempt_count=attempts, next_attempt_at=next_at)
    session.add(row)
    session.commit()
    return row.id


def get(session, row_id):
    session.expire_all()
    return session.get(PRReview, row_id)


def run(fake, now=NOW):
    return asyncio.run(retry_queue.run_once(fake.review, fake.check, now=now))


def test_recovers_when_ollama_is_back(db_session):
    row_id = add_row(db_session)
    fake = FakeOllama([OK])
    assert run(fake) == 1
    row = get(db_session, row_id)
    assert row.status == "reviewed"
    assert row.nitpick_count == 1
    assert row.attempt_count == 2
    assert row.last_error is None
    assert row.next_attempt_at is None


def test_does_nothing_while_unreachable(db_session):
    row_id = add_row(db_session)
    fake = FakeOllama([OK])
    fake.reachable = False
    assert run(fake) == 0
    assert fake.calls == 0
    assert get(db_session, row_id).attempt_count == 1


def test_timeout_rows_are_retried(db_session):
    row_id = add_row(db_session, status="llm_timeout")
    run(FakeOllama([OK]))
    assert get(db_session, row_id).status == "reviewed"


def test_exponential_backoff(db_session):
    row_id = add_row(db_session, attempts=0)
    fake = FakeOllama([TIMEOUT, TIMEOUT, OK])

    run(fake, NOW)
    row = get(db_session, row_id)
    assert row.attempt_count == 1
    assert row.status == "llm_timeout"
    assert row.last_error == "timed out after 10s"
    assert row.next_attempt_at == NOW + timedelta(seconds=60)

    assert run(fake, NOW + timedelta(seconds=59)) == 0  # not due yet

    run(fake, NOW + timedelta(seconds=60))
    row = get(db_session, row_id)
    assert row.attempt_count == 2
    assert row.next_attempt_at == NOW + timedelta(seconds=60 + 120)


def test_backoff_is_capped(monkeypatch):
    monkeypatch.setenv("RETRY_BACKOFF_MAX_SECONDS", "100")
    assert retry_queue.backoff_seconds(1) == 60
    assert retry_queue.backoff_seconds(2) == 100
    assert retry_queue.backoff_seconds(10) == 100


def test_max_attempts_moves_row_to_failed(db_session):
    row_id = add_row(db_session, attempts=2)
    fake = FakeOllama([OFFLINE, OK])
    run(fake)
    row = get(db_session, row_id)
    assert row.attempt_count == 3
    assert row.status == "llm_failed"
    assert row.last_error.startswith("unreachable")
    # Exhausted rows are never picked up again.
    assert run(fake, NOW + timedelta(days=1)) == 0
    assert fake.calls == 1


def test_reviewed_and_diffless_rows_are_skipped(db_session):
    add_row(db_session, status="reviewed")
    add_row(db_session, status="llm_offline_pending", diff=None)
    assert run(FakeOllama([])) == 0


def test_review_exception_is_recorded_not_raised(db_session):
    row_id = add_row(db_session, attempts=0)

    async def boom(_code):
        raise RuntimeError("bad")

    async def up():
        return True

    asyncio.run(retry_queue.run_once(boom, up, now=NOW))
    row = get(db_session, row_id)
    assert row.status == "llm_error"
    assert "RuntimeError" in row.last_error


def test_worker_loop_stops(db_url):
    async def scenario():
        stop = asyncio.Event()
        fake = FakeOllama([])
        task = asyncio.create_task(retry_queue.worker_loop(fake.review, fake.check, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_migration_on_existing_db_keeps_rows_and_enables_retry(tmp_path):
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pr_reviews (id INTEGER PRIMARY KEY, repo_name VARCHAR NOT NULL, "
        "pr_number INTEGER NOT NULL, pr_title VARCHAR NOT NULL, pr_url VARCHAR NOT NULL, "
        "diff_text TEXT, review_text TEXT, critical_count INTEGER NOT NULL, "
        "warning_count INTEGER NOT NULL, nitpick_count INTEGER NOT NULL, "
        "status VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
    )
    conn.execute(
        "INSERT INTO pr_reviews VALUES (1, 'o/r', 3, 'old', 'u', '+y = 2', NULL, 0, 0, 0, "
        "'llm_offline_pending', '2025-06-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO pr_reviews VALUES (2, 'o/r', 4, 'done', 'u', '+z', 'WARNING: kept', 0, 1, 0, "
        "'reviewed', '2025-06-02 00:00:00')"
    )
    conn.commit()
    conn.close()

    database.configure(f"sqlite:///{path.as_posix()}")
    database.init_db()
    try:
        session = database.SessionLocal()
        old = session.get(PRReview, 1)
        kept = session.get(PRReview, 2)
        assert old.attempt_count == 0 and old.last_error is None and old.next_attempt_at is None
        assert kept.review_text == "WARNING: kept" and kept.warning_count == 1
        session.close()

        run(FakeOllama([OK]))
        session = database.SessionLocal()
        assert session.get(PRReview, 1).status == "reviewed"
        assert session.get(PRReview, 2).review_text == "WARNING: kept"
        session.close()
        # Running the migration again is a no-op.
        assert database.migrate() == []
    finally:
        database.engine.dispose()
    assert list(tmp_path.glob("audit.db.bak-*"))


# --- status classification in the Ollama client (mocked transport, no network)


def _patch_ollama_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx, "AsyncClient",
        functools.partial(real_client, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "exc,status",
    [
        (httpx.ConnectError("refused"), "llm_offline_pending"),
        (httpx.ConnectTimeout("connect timeout"), "llm_offline_pending"),
        (httpx.ReadTimeout("slow"), "llm_timeout"),
        (httpx.RemoteProtocolError("dropped"), "llm_error"),
    ],
)
def test_ollama_failures_are_classified(monkeypatch, exc, status):
    def handler(request):
        raise exc

    _patch_ollama_transport(monkeypatch, handler)
    result = asyncio.run(main.review_code_with_ollama("x = 1"))
    assert result["status"] == status
    assert result["error"]


def test_ollama_http_error_is_llm_error(monkeypatch):
    _patch_ollama_transport(monkeypatch, lambda request: httpx.Response(500))
    result = asyncio.run(main.review_code_with_ollama("x = 1"))
    assert result["status"] == "llm_error"
    assert "HTTP 500" in result["error"]
