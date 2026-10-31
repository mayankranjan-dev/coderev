import asyncio

import pytest

import main
from models import PRReview
from reviewer import engine
from reviewer.engine import EngineConfig, run_review
from reviewer.providers import HTTP_ERROR, TIMEOUT, UNREACHABLE, ProviderError, StubProvider

GOOD = "WARNING: a.py:1 m Correction: c"
GARBAGE = "I think this code is fine, mostly."


def review(stub, text="x = 1", **config):
    return asyncio.run(run_review(text, stub, lambda code: f"P:{code}", EngineConfig(**config)))


@pytest.mark.parametrize(
    "error,kind",
    [
        (ProviderError(TIMEOUT, "slow"), engine.TIMEOUT),
        (ProviderError(UNREACHABLE, "down"), engine.UNREACHABLE),
        (ProviderError(HTTP_ERROR, "HTTP 500"), engine.PROVIDER_ERROR),
    ],
)
def test_transport_failures_are_typed_and_not_retried(error, kind):
    stub = StubProvider(replies=[error, GOOD])
    result = review(stub, max_retries=3)
    assert result.failure.kind == kind
    assert len(stub.calls) == 1
    assert result.findings == []


def test_malformed_output_is_retried_once_with_next_seed():
    stub = StubProvider(replies=[GARBAGE, GOOD])
    result = review(stub, seed=10, max_retries=1)
    assert result.ok
    assert [seed for _, seed in stub.calls] == [10, 11]
    assert result.retries == 1
    assert [a.parse_status for a in result.chunks[0].attempts] == ["malformed", "ok"]
    assert result.findings[0].file == "a.py"


def test_retry_is_bounded():
    stub = StubProvider(replies=[GARBAGE])
    result = review(stub, max_retries=1)
    assert len(stub.calls) == 2
    assert result.failure.kind == engine.MALFORMED_OUTPUT
    assert result.raw_text == GARBAGE


def test_retry_off_means_one_call():
    stub = StubProvider(replies=[GARBAGE, GOOD])
    result = review(stub, max_retries=0)
    assert len(stub.calls) == 1 and result.failure.kind == engine.MALFORMED_OUTPUT


def test_empty_output_is_typed():
    stub = StubProvider(replies=["   \n"])
    result = review(stub, max_retries=1)
    assert result.failure.kind == engine.EMPTY_OUTPUT
    assert len(stub.calls) == 2


def test_no_issues_sentinel_is_success_without_retry():
    stub = StubProvider(replies=["NO_ISSUES_FOUND", GOOD])
    result = review(stub, max_retries=2)
    assert result.ok and result.findings == [] and len(stub.calls) == 1


def test_seed_none_stays_none_on_retry():
    stub = StubProvider(replies=[GARBAGE, GOOD])
    review(stub, max_retries=1)
    assert [seed for _, seed in stub.calls] == [None, None]


def test_unexpected_provider_exception_does_not_escape():
    def explode(prompt, seed):
        raise KeyError("boom")

    result = review(StubProvider(replies=[explode]))
    assert result.failure.kind == engine.PROVIDER_ERROR
    assert "KeyError" in result.failure.detail


def test_prompt_builder_exception_does_not_escape():
    def bad_builder(code):
        raise RuntimeError("template missing")

    result = asyncio.run(run_review("x", StubProvider(), bad_builder))
    assert result.failure is not None


def test_bad_chunking_config_does_not_escape():
    result = review(StubProvider(), chunking="sideways")
    assert result.failure.kind == engine.PROVIDER_ERROR
    assert "planning failed" in result.failure.detail


def test_per_file_partial_output_failure_is_not_a_review_failure():
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    stub = StubProvider(replies=[GOOD, GARBAGE, GARBAGE])
    result = review(stub, text=diff, chunking="per_file", max_retries=1)
    assert result.ok
    assert len(result.findings) == 1
    assert result.chunks[1].failure.kind == engine.MALFORMED_OUTPUT


# --- production mapping and storage ------------------------------------------


@pytest.mark.parametrize(
    "replies,status,has_text",
    [
        ([GARBAGE], "llm_malformed_output", True),
        (["  "], "llm_empty_output", False),
        ([ProviderError(TIMEOUT, "slow")], "llm_timeout", False),
        ([ProviderError(UNREACHABLE, "down")], "llm_offline_pending", False),
    ],
)
def test_failure_reason_is_stored(client, auth, db_session, monkeypatch, replies, status, has_text):
    stub = StubProvider(replies=replies)
    # Undo the conftest stub so the real review path runs on the stub provider.
    monkeypatch.setattr(main, "review_code_with_ollama", REAL_REVIEW)
    monkeypatch.setattr(main, "get_review_provider", lambda: stub)
    body = client.post("/api/reviews/manual", json={"code_snippet": "x = 1"}, headers=auth).json()
    assert body["status"] == status
    row = db_session.get(PRReview, body["id"])
    assert row.last_error and row.last_error.split(":")[0] in {
        "malformed_output", "empty_output", "timeout", "unreachable"
    }
    assert (row.review_text is not None) == has_text


REAL_REVIEW = main.review_code_with_ollama


def test_output_failures_are_not_requeued():
    import retry_queue

    assert "llm_malformed_output" not in retry_queue.RETRYABLE_STATUSES
    assert "llm_empty_output" not in retry_queue.RETRYABLE_STATUSES
