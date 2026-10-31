import pytest

import security
from security import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    limiter = RateLimiter(clock)
    assert all(limiter.hit("b", "1.2.3.4", 3, 60) is None for _ in range(3))
    assert limiter.hit("b", "1.2.3.4", 3, 60) == 60


def test_limiter_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(clock)
    limiter.hit("b", "ip", 1, 60)
    clock.now += 45
    assert limiter.hit("b", "ip", 1, 60) == 15
    clock.now += 15
    assert limiter.hit("b", "ip", 1, 60) is None


def test_limiter_is_per_ip_and_per_bucket():
    limiter = RateLimiter(FakeClock())
    assert limiter.hit("b", "ip-a", 1, 60) is None
    assert limiter.hit("b", "ip-b", 1, 60) is None
    assert limiter.hit("other", "ip-a", 1, 60) is None
    assert limiter.hit("b", "ip-a", 1, 60) is not None


@pytest.mark.parametrize(
    "path,body,env",
    [
        ("/api/execute", {"code": "print(1)"}, "RATE_LIMIT_EXECUTE_PER_MINUTE"),
        ("/api/reviews/manual", {"code_snippet": "x = 1"}, "RATE_LIMIT_REVIEW_PER_MINUTE"),
    ],
)
def test_endpoint_returns_429_with_retry_after(client, auth, monkeypatch, path, body, env):
    monkeypatch.setenv(env, "2")
    assert client.post(path, json=body, headers=auth).status_code == 200
    assert client.post(path, json=body, headers=auth).status_code == 200
    blocked = client.post(path, json=body, headers=auth)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_wrong_keys_count_against_the_limit(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXECUTE_PER_MINUTE", "1")
    bad = {"X-API-Key": "guess"}
    assert client.post("/api/execute", json={"code": "1"}, headers=bad).status_code == 403
    assert client.post("/api/execute", json={"code": "1"}, headers=bad).status_code == 429


def test_zero_limit_disables(client, auth, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXECUTE_PER_MINUTE", "0")
    for _ in range(15):
        assert client.post("/api/execute", json={"code": "1"}, headers=auth).status_code == 200


def test_reviews_list_is_not_rate_limited(client, auth):
    for _ in range(20):
        assert client.get("/api/reviews", headers=auth).status_code == 200


def test_cors_default_allows_dev_origin_only(client):
    ok = client.options(
        "/api/reviews",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"

    evil = client.options(
        "/api/reviews",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in evil.headers


def test_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", " https://a.example , *,https://b.example ")
    assert security.cors_origins_from_env() == ["https://a.example", "https://b.example"]
    monkeypatch.delenv("CORS_ALLOW_ORIGINS")
    assert security.cors_origins_from_env() == ["http://localhost:5173", "http://127.0.0.1:5173"]
