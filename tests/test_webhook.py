import hashlib
import hmac
import json
import sqlite3

import pytest

import database
from conftest import TEST_WEBHOOK_SECRET
from models import PRReview


def make_payload(action="opened", sha="a" * 40, number=7):
    return {
        "action": action,
        "number": number,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": number,
            "title": "Some change",
            "html_url": f"https://github.com/owner/repo/pull/{number}",
            "diff_url": f"https://github.com/owner/repo/pull/{number}.diff",
            "head": {"sha": sha},
        },
    }


def sign(body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client, payload, event="pull_request", signature=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature != "omit":
        headers["X-Hub-Signature-256"] = signature or sign(body)
    return client.post("/api/webhook/github", content=body, headers=headers)


def rows(db_session):
    db_session.expire_all()
    return db_session.query(PRReview).all()


def test_valid_signature_accepts_and_processes_in_background(client, db_session, ollama_stub, diff_stub):
    ollama_stub.result = {
        "review_text": "CRITICAL: x.py:1 bad Correction: fix",
        "critical_count": 1,
        "warning_count": 0,
        "nitpick_count": 0,
        "status": "reviewed",
    }
    response = post(client, make_payload())
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    [row] = rows(db_session)
    assert row.head_sha == "a" * 40
    assert row.status == "reviewed"
    assert row.critical_count == 1
    assert diff_stub.calls == ["https://github.com/owner/repo/pull/7.diff"]


def test_invalid_signature_rejected(client, db_session):
    response = post(client, make_payload(), signature="sha256=" + "0" * 64)
    assert response.status_code == 401
    assert rows(db_session) == []


def test_signature_with_wrong_secret_rejected(client, db_session):
    body = json.dumps(make_payload()).encode()
    response = post(client, None, raw=body, signature=sign(body, "other-secret"))
    assert response.status_code == 401


def test_missing_signature_rejected(client, db_session):
    response = post(client, make_payload(), signature="omit")
    assert response.status_code == 401
    assert rows(db_session) == []


def test_unset_secret_fails_closed(client, db_session, monkeypatch):
    import main

    monkeypatch.setattr(main, "WEBHOOK_SECRET", "")
    assert post(client, make_payload()).status_code == 401


def test_wrong_event_ignored(client, db_session):
    response = post(client, make_payload(), event="push")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert rows(db_session) == []


@pytest.mark.parametrize("action", ["closed", "labeled", "edited", None])
def test_wrong_action_ignored(client, db_session, action):
    response = post(client, make_payload(action=action))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert rows(db_session) == []


def test_redelivery_creates_one_row(client, db_session, ollama_stub):
    payload = make_payload()
    first = post(client, payload)
    second = post(client, payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(rows(db_session)) == 1
    assert len(ollama_stub.calls) == 1


def test_new_head_sha_creates_new_row(client, db_session):
    post(client, make_payload(sha="a" * 40))
    post(client, make_payload(action="synchronize", sha="b" * 40))
    assert len(rows(db_session)) == 2


def test_dedupe_is_enforced_by_the_database(db_session):
    from sqlalchemy.exc import IntegrityError

    for _ in range(2):
        db_session.add(PRReview(repo_name="o/r", pr_number=1, pr_title="", pr_url="", head_sha="c"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_malformed_json_returns_400(client):
    response = post(client, None, raw=b"{not json")
    assert response.status_code == 400


def test_missing_head_sha_returns_400(client, db_session):
    payload = make_payload()
    del payload["pull_request"]["head"]
    assert post(client, payload).status_code == 400
    assert rows(db_session) == []


def test_missing_diff_marks_row_diff_unavailable(client, db_session, diff_stub, ollama_stub):
    diff_stub.text = None
    post(client, make_payload())
    [row] = rows(db_session)
    assert row.status == "diff_unavailable"
    assert ollama_stub.calls == []


def test_background_failure_does_not_escape(client, db_session, monkeypatch):
    import main

    async def boom(_):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main, "review_code_with_ollama", boom)
    assert post(client, make_payload()).status_code == 202
    [row] = rows(db_session)
    assert row.status == "pending"


def test_migration_adds_head_sha_to_existing_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pr_reviews (id INTEGER PRIMARY KEY, repo_name VARCHAR NOT NULL, "
        "pr_number INTEGER NOT NULL, pr_title VARCHAR NOT NULL, pr_url VARCHAR NOT NULL, "
        "diff_text TEXT, review_text TEXT, critical_count INTEGER NOT NULL, "
        "warning_count INTEGER NOT NULL, nitpick_count INTEGER NOT NULL, "
        "status VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
    )
    for _ in range(2):  # pre-existing duplicates must survive the unique index
        conn.execute(
            "INSERT INTO pr_reviews (repo_name, pr_number, pr_title, pr_url, critical_count, "
            "warning_count, nitpick_count, status, created_at) "
            "VALUES ('o/r', 1, 't', 'u', 0, 0, 0, 'reviewed', '2026-01-01 00:00:00')"
        )
    conn.commit()
    conn.close()

    database.configure(f"sqlite:///{path.as_posix()}")
    database.init_db()
    database.engine.dispose()

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pr_reviews)")}
    count = conn.execute("SELECT COUNT(*) FROM pr_reviews").fetchone()[0]
    conn.close()
    assert "head_sha" in columns
    assert count == 2
    assert list(tmp_path.glob("old.db.bak-*")), "backup was not written"
