"""GET /api/reviews pagination: limit, offset, bounds, order, total header."""

from datetime import datetime, timedelta, timezone

import pytest

import main
from models import PRReview


@pytest.fixture
def many(db_session):
    """25 rows; row n is n minutes newer than row 1."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(1, 26):
        db_session.add(PRReview(
            repo_name="o/r", pr_number=n, pr_title=f"t{n}", pr_url="u", head_sha=f"sha{n}",
            status="reviewed", created_at=base + timedelta(minutes=n),
        ))
    db_session.commit()


def numbers(response):
    return [row["pr_number"] for row in response.json()]


def test_default_limit_applies(client, auth, many, monkeypatch):
    monkeypatch.setattr(main, "REVIEWS_DEFAULT_LIMIT", 5)
    response = client.get("/api/reviews", headers=auth)
    assert response.status_code == 200
    assert numbers(response) == [25, 24, 23, 22, 21]


def test_default_limit_is_bounded_by_the_maximum():
    assert 1 <= main.REVIEWS_DEFAULT_LIMIT <= main.REVIEWS_MAX_LIMIT


def test_limit_and_offset_walk_newest_first_without_gaps(client, auth, many):
    pages = [numbers(client.get(f"/api/reviews?limit=10&offset={o}", headers=auth)) for o in (0, 10, 20)]
    assert pages[0] == list(range(25, 15, -1))
    assert pages[1] == list(range(15, 5, -1))
    assert pages[2] == [5, 4, 3, 2, 1]


def test_total_count_header(client, auth, many):
    response = client.get("/api/reviews?limit=3", headers=auth)
    assert response.headers["X-Total-Count"] == "25"
    assert len(response.json()) == 3


def test_offset_past_the_end_is_an_empty_list(client, auth, many):
    response = client.get("/api/reviews?offset=100", headers=auth)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "25"


def test_empty_database_still_returns_an_empty_list(client, auth):
    response = client.get("/api/reviews", headers=auth)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "offset=-1", "limit=abc", "limit=%d"])
def test_invalid_paging_parameters_are_422(client, auth, query):
    query = query % (main.REVIEWS_MAX_LIMIT + 1) if "%d" in query else query
    assert client.get(f"/api/reviews?{query}", headers=auth).status_code == 422


def test_limit_at_the_maximum_is_accepted(client, auth, many):
    assert client.get(f"/api/reviews?limit={main.REVIEWS_MAX_LIMIT}", headers=auth).status_code == 200


def test_equal_timestamps_do_not_repeat_or_skip_rows(client, auth, db_session):
    same = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(1, 8):
        db_session.add(PRReview(repo_name="o/r", pr_number=n, pr_title="t", pr_url="u", head_sha=f"s{n}",
                                status="reviewed", created_at=same))
    db_session.commit()
    seen = []
    for offset in (0, 3, 6):
        seen += numbers(client.get(f"/api/reviews?limit=3&offset={offset}", headers=auth))
    assert sorted(seen) == list(range(1, 8))


def test_auth_is_still_required(client, many):
    assert client.get("/api/reviews?limit=1").status_code == 403
