"""
Send a signed, fake pull_request webhook to a locally running CodeRev Bot.

The signing secret is read from WEBHOOK_SECRET (environment or .env), the same
variable the server uses, so the two always agree. Nothing is hardcoded.

Usage:
    python simulate_pr.py [--url URL] [--sha HEAD_SHA]

The server dedupes on (repo, PR number, head SHA), so sending the same SHA
twice returns "duplicate". Pass a different --sha to create a new review.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys

import httpx
from dotenv import load_dotenv

DEFAULT_URL = "http://127.0.0.1:8000/api/webhook/github"


def build_payload(head_sha: str) -> dict:
    # Shape mirrors a real "pull_request" event: repo info lives in the
    # top-level "repository" object, and "pull_request.html_url"/"diff_url"
    # are both present, same as GitHub actually sends.
    return {
        "action": "opened",
        "number": 42,
        "repository": {
            "name": "fastapi",
            "full_name": "fastapi/fastapi",
        },
        "pull_request": {
            "number": 42,
            "title": "Fix: Implement exponential backoff for DB retries",
            "html_url": "https://github.com/fastapi/fastapi/pull/10000",
            # Real public diff URL for testing
            "diff_url": "https://github.com/fastapi/fastapi/pull/10000.diff",
            "head": {"sha": head_sha},
            "base": {"repo": {"name": "coderev-bot", "owner": {"login": "mayankranjan-dev"}}},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--sha", default="f3g4h5j6k7l8", help="head SHA to send")
    args = parser.parse_args()

    load_dotenv()
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        print("WEBHOOK_SECRET is not set; the server would reject an unsigned webhook.", file=sys.stderr)
        return 2

    body = json.dumps(build_payload(args.sha)).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }

    print(f"Sending signed webhook to {args.url}...")
    try:
        response = httpx.post(args.url, content=body, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
