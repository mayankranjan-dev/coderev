import json

from models import PRReview


def test_manual_review_returns_structured_findings(client, auth, ollama_stub):
    ollama_stub.result = {
        "review_text": "CRITICAL: snippet:2 bug Correction: fix",
        "critical_count": 1, "warning_count": 0, "nitpick_count": 0,
        "findings": [{"severity": "CRITICAL", "file": "snippet", "line_start": 2, "line_end": 2,
                      "message": "bug", "correction": "fix"}],
        "status": "reviewed", "error": None,
    }
    body = client.post("/api/reviews/manual", json={"code_snippet": "x"}, headers=auth).json()
    assert body["findings"] == [{"severity": "CRITICAL", "file": "snippet", "line_start": 2,
                                 "line_end": 2, "message": "bug", "correction": "fix"}]
    assert body["critical_count"] == 1


def test_legacy_rows_are_parsed_from_review_text(client, auth, db_session):
    db_session.add(PRReview(repo_name="o/r", pr_number=1, pr_title="t", pr_url="u",
                            review_text="WARNING: old style finding Correction: do x",
                            warning_count=1, status="reviewed"))
    db_session.commit()
    [row] = client.get("/api/reviews", headers=auth).json()
    assert row["findings"][0]["severity"] == "WARNING"
    assert row["findings"][0]["file"] is None
    assert row["findings"][0]["correction"] == "do x"


def test_corrupt_findings_json_falls_back_to_text(client, auth, db_session):
    db_session.add(PRReview(repo_name="o/r", pr_number=1, pr_title="t", pr_url="u",
                            review_text="NITPICK: a.py:1 m", findings_json="{not json",
                            status="reviewed"))
    db_session.commit()
    [row] = client.get("/api/reviews", headers=auth).json()
    assert row["findings"][0]["file"] == "a.py"


def test_review_function_parses_model_output(monkeypatch):
    import asyncio

    import main
    from reviewer.providers import StubProvider

    stub = StubProvider(replies=["1. **CRITICAL:** a.py:3-4 bad Correction: good\nchatter"])
    monkeypatch.setattr(main, "get_review_provider", lambda: stub)
    result = asyncio.run(main.review_code_with_ollama("diff"))
    assert result["status"] == "reviewed"
    assert result["critical_count"] == 1
    assert result["findings"][0]["line_end"] == 4
    assert json.dumps(result["findings"])  # serializable for findings_json
