# Known Limitations — SQLite on Render free tier is ephemeral; no retry queue for offline reviews yet.
"""
CodeRev Bot — a FastAPI service that listens for GitHub pull_request webhooks,
fetches the diff, sends it to a locally-running Ollama model for review, and
records the result (or a "pending" placeholder if the LLM is unreachable).

Design notes:
- Webhook signature verification is hand-rolled with hmac/hashlib per the
  GitHub docs (https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).
  No third-party HMAC/webhook-verification packages are used.
- Diff retrieval uses the PR's `diff_url` (e.g. https://github.com/{owner}/{repo}/pull/{n}.diff)
  rather than the Files API. `diff_url` returns the full unified diff as plain
  text in a single unauthenticated GET, which is simpler and cheaper than
  paginating the Files API and reassembling per-file patches. The tradeoff is
  that it only works for diffs GitHub is willing to serve anonymously (fine
  for public repos; private repos would need an Authorization header, which
  would be added here as a token from the environment).
- Ollama is treated as an unreliable, optional dependency: any connection or
  timeout failure is logged and results in a "llm_offline_pending" row rather
  than a request failure. This is the core reliability requirement of the
  service — a dead LLM must never crash the webhook handler or lose the PR.
"""

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine, get_db
from models import PRReview

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("coderev_bot")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
OLLAMA_TIMEOUT_SECONDS = 10.0

# Create tables on startup. For a production system with migrations this
# would be replaced by Alembic, but that's out of scope here.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="CodeRev Bot", version="1.0.0")


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class ReviewOut(BaseModel):
    id: int
    repo_name: str
    pr_number: int
    pr_title: str
    pr_url: str
    diff_text: Optional[str] = None
    review_text: Optional[str] = None
    critical_count: int
    warning_count: int
    nitpick_count: int
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class HealthOut(BaseModel):
    status: str
    ollama_reachable: bool


# ---------------------------------------------------------------------------
# Webhook signature verification (hand-written, no libraries)
# ---------------------------------------------------------------------------

def verify_github_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Verify the X-Hub-Signature-256 header against the raw request body using
    HMAC-SHA256, per GitHub's webhook validation scheme.

    Returns True iff the signature is present, well-formed, and matches.
    Uses hmac.compare_digest for constant-time comparison — never `==`.
    """
    if not signature_header:
        return False

    if not WEBHOOK_SECRET:
        # Misconfiguration: no secret configured server-side. Fail closed.
        logger.warning("WEBHOOK_SECRET is not set; rejecting webhook.")
        return False

    expected_signature = (
        "sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected_signature, signature_header)


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------

def build_review_prompt(diff_text: str) -> str:
    return (
        "You are a senior code reviewer. Review the following unified diff. "
        "List every finding as a single line prefixed with one of: "
        "CRITICAL:, WARNING:, or NITPICK:. Use CRITICAL for bugs, security "
        "issues, or correctness problems; WARNING for design or maintainability "
        "concerns; NITPICK for style/formatting. One finding per line, no other "
        "commentary or preamble.\n\n"
        f"Diff:\n{diff_text}"
    )


def parse_severity_counts(review_text: str) -> dict:
    """Simple line-prefix parser for CRITICAL/WARNING/NITPICK counts."""
    counts = {"critical_count": 0, "warning_count": 0, "nitpick_count": 0}
    for line in review_text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("CRITICAL:") or stripped.startswith("CRITICAL "):
            counts["critical_count"] += 1
        elif stripped.startswith("WARNING:") or stripped.startswith("WARNING "):
            counts["warning_count"] += 1
        elif stripped.startswith("NITPICK:") or stripped.startswith("NITPICK "):
            counts["nitpick_count"] += 1
    return counts


async def request_ollama_review(diff_text: str) -> Optional[str]:
    """
    Send the diff to Ollama for review.

    Returns the model's response text on success, or None if Ollama is
    unreachable/timed out. This path (Ollama offline) is an expected,
    routine condition in production — not an error — so it's logged at
    INFO level and handled gracefully rather than raising.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_review_prompt(diff_text),
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("response", "")
    except (httpx.ConnectError, httpx.TimeoutException):
        logger.info("Ollama is unreachable at %s; queuing review as llm_offline_pending.", OLLAMA_BASE_URL)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("Ollama returned an error status: %s", exc)
        return None


async def check_ollama_reachable() -> bool:
    """Real connectivity check against Ollama's /api/tags endpoint."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            return response.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


# ---------------------------------------------------------------------------
# GitHub payload extraction / diff fetching
# ---------------------------------------------------------------------------

async def fetch_diff_text(diff_url: str) -> Optional[str]:
    """Fetch the unified diff text from GitHub's diff_url."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(diff_url)
            response.raise_for_status()
            return response.text
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
        logger.warning("Failed to fetch diff from %s: %s", diff_url, exc)
        return None


async def process_pull_request_event(payload: dict) -> None:
    """
    Handle an opened/synchronize pull_request event: persist a pending row
    immediately, then attempt an Ollama review and update the row in place.
    """
    pull_request = payload.get("pull_request", {})
    repository = payload.get("repository", {})

    repo_name = repository.get("full_name", "unknown/unknown")
    pr_number = pull_request.get("number", payload.get("number", 0))
    pr_title = pull_request.get("title", "")
    pr_url = pull_request.get("html_url", "")
    diff_url = pull_request.get("diff_url", "")

    db: Session = SessionLocal()
    try:
        review = PRReview(
            repo_name=repo_name,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_url=pr_url,
            diff_text=None,
            review_text=None,
            status="pending",
        )
        db.add(review)
        db.commit()
        db.refresh(review)

        diff_text = await fetch_diff_text(diff_url) if diff_url else None
        review.diff_text = diff_text
        db.commit()

        if not diff_text:
            logger.warning("No diff text available for PR #%s in %s; leaving as pending.", pr_number, repo_name)
            db.commit()
            return

        review_text = await request_ollama_review(diff_text)

        if review_text is None:
            review.status = "llm_offline_pending"
            db.commit()
            return

        counts = parse_severity_counts(review_text)
        review.review_text = review_text
        review.critical_count = counts["critical_count"]
        review.warning_count = counts["warning_count"]
        review.nitpick_count = counts["nitpick_count"]
        review.status = "reviewed"
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: Optional[str] = Header(default=None, alias="X-GitHub-Event"),
):
    # Raw bytes MUST be read before any JSON parsing, since signature
    # verification is over the exact bytes GitHub sent.
    raw_body = await request.body()

    if not x_hub_signature_256:
        return JSONResponse(status_code=401, content={"detail": "Missing X-Hub-Signature-256 header"})

    if not verify_github_signature(raw_body, x_hub_signature_256):
        return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

    payload = await request.json()

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "not a pull_request event"}

    action = payload.get("action")
    if action not in ("opened", "synchronize"):
        return {"status": "ignored", "reason": f"action '{action}' not handled"}

    await process_pull_request_event(payload)

    return {"status": "accepted"}


@app.get("/api/reviews", response_model=list[ReviewOut])
def get_reviews(db: Session = Depends(get_db)):
    reviews = db.query(PRReview).order_by(PRReview.created_at.desc()).all()
    return reviews


@app.get("/api/health", response_model=HealthOut)
async def health():
    ollama_reachable = await check_ollama_reachable()
    return HealthOut(status="ok", ollama_reachable=ollama_reachable)
