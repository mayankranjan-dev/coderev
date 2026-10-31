"""
Request-level access control for CodeRev Bot.

API key: the review and execute endpoints require an X-API-Key header that
matches CODEREV_API_KEY. The env var is read on every request so rotating it
needs no code change, and an unset or empty value fails closed (403) instead of
leaving the endpoints open. The webhook keeps its own HMAC check and
/api/health stays public.
"""

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException

API_KEY_ENV = "CODEREV_API_KEY"


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv(API_KEY_ENV, "")
    if not expected:
        # Misconfiguration: refuse everything rather than run unauthenticated.
        raise HTTPException(status_code=403, detail="API key is not configured on the server.")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Missing or invalid API key.")
