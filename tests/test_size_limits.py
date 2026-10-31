"""Size limits: manual submissions (HTTP 413) and fetched diffs (typed failure)."""

import asyncio
import functools

import httpx
import pytest

import main
from models import PRReview
from test_webhook import make_payload, post


def _transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx, "AsyncClient",
        functools.partial(real_client, transport=httpx.MockTransport(handler)),
    )


# --- manual submissions


def test_manual_review_over_limit_is_413(client, auth, ollama_stub, monkeypatch):
    monkeypatch.setattr(main, "MAX_REVIEW_CODE_CHARS", 100)
    response = client.post("/api/reviews/manual", json={"code_snippet": "x" * 101}, headers=auth)
    assert response.status_code == 413
    assert "100" in response.json()["detail"]
    assert ollama_stub.calls == []


def test_manual_review_at_limit_is_accepted(client, auth, ollama_stub, monkeypatch):
    monkeypatch.setattr(main, "MAX_REVIEW_CODE_CHARS", 100)
    response = client.post("/api/reviews/manual", json={"code_snippet": "x" * 100}, headers=auth)
    assert response.status_code == 200
    assert len(ollama_stub.calls) == 1


def test_oversized_manual_review_stores_no_row(client, auth, db_session, monkeypatch):
    monkeypatch.setattr(main, "MAX_REVIEW_CODE_CHARS", 10)
    client.post("/api/reviews/manual", json={"code_snippet": "x" * 11}, headers=auth)
    assert db_session.query(PRReview).count() == 0


# --- fetched diffs


def test_diff_within_limit_is_returned(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, text="diff --git a/x b/x\n"))
    monkeypatch.setattr(main, "MAX_DIFF_BYTES", 1000)
    assert asyncio.run(_real_fetch()) == "diff --git a/x b/x\n"


def test_diff_over_declared_content_length_is_rejected(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 2000))
    monkeypatch.setattr(main, "MAX_DIFF_BYTES", 1000)
    with pytest.raises(main.DiffTooLarge):
        asyncio.run(_real_fetch())


def test_streamed_diff_over_limit_is_rejected_without_content_length(monkeypatch):
    async def body():
        for _ in range(4):
            yield b"x" * 400

    _transport(monkeypatch, lambda request: httpx.Response(200, content=body()))
    monkeypatch.setattr(main, "MAX_DIFF_BYTES", 1000)
    with pytest.raises(main.DiffTooLarge):
        asyncio.run(_real_fetch())


def test_http_error_still_returns_none(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(404))
    assert asyncio.run(_real_fetch()) is None


def test_oversized_diff_marks_row_diff_too_large(client, db_session, ollama_stub, monkeypatch):
    async def too_big(_url):
        raise main.DiffTooLarge("test")

    monkeypatch.setattr(main, "fetch_diff_text", too_big)
    assert post(client, make_payload()).status_code == 202
    db_session.expire_all()
    [row] = db_session.query(PRReview).all()
    assert row.status == "diff_too_large"
    assert row.diff_text is None
    assert row.last_error and "byte limit" in row.last_error
    assert ollama_stub.calls == []


def test_diff_too_large_rows_are_not_retried():
    import retry_queue

    assert "diff_too_large" not in retry_queue.RETRYABLE_STATUSES


async def _real_fetch():
    return await main.fetch_diff_text("https://github.com/o/r/pull/1.diff")
