"""
Request-level access control for CodeRev Bot.

API key: the review and execute endpoints require an X-API-Key header that
matches CODEREV_API_KEY. The env var is read on every request so rotating it
needs no code change, and an unset or empty value fails closed (403) instead of
leaving the endpoints open. The webhook keeps its own HMAC check and
/api/health stays public.
"""

import hmac
import math
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

from fastapi import Header, HTTPException, Request

API_KEY_ENV = "CODEREV_API_KEY"

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def cors_origins_from_env() -> list[str]:
    """
    Allowed CORS origins from CORS_ALLOW_ORIGINS (comma-separated). Defaults to
    the Vite dev server only. A literal "*" is dropped: with credentials-style
    headers (X-API-Key) a wildcard would let any site drive the API from a
    user's browser.
    """
    raw = os.getenv("CORS_ALLOW_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip() and origin.strip() != "*"]


class RateLimiter:
    """
    In-memory sliding-window limiter keyed by (bucket, client IP).

    Single-process only: counts are not shared across workers and reset on
    restart. That fits the current one-process deployment. The client IP is
    the socket peer address; X-Forwarded-For is not trusted, because without a
    known proxy in front any caller could forge it to dodge the limit.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def hit(self, bucket: str, client: str, limit: int, window: float) -> Optional[int]:
        """Record a request. Returns None if allowed, else Retry-After seconds."""
        now = self._clock()
        key = (bucket, client)
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            # Drop idle keys so memory doesn't grow with every IP ever seen.
            if len(self._hits) > 10_000:
                for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
                    del self._hits[stale]
            return None


limiter = RateLimiter()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def rate_limit(bucket: str, limit_env: str, default_limit: int):
    """
    Dependency factory. Limit and window are read per request from
    `limit_env` and RATE_LIMIT_WINDOW_SECONDS. A limit of 0 or less disables
    the check for that bucket.
    """

    def dependency(request: Request) -> None:
        limit = _env_int(limit_env, default_limit)
        if limit <= 0:
            return
        window = max(1, _env_int("RATE_LIMIT_WINDOW_SECONDS", 60))
        client = request.client.host if request.client else "unknown"
        retry_after = limiter.hit(bucket, client, limit, window)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv(API_KEY_ENV, "")
    if not expected:
        # Misconfiguration: refuse everything rather than run unauthenticated.
        raise HTTPException(status_code=403, detail="API key is not configured on the server.")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Missing or invalid API key.")
