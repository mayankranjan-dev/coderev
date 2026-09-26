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
from fastapi import Depends, FastAPI, Header, HTTPException, Request
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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
# Default 10s per the original spec. CPU-only machines can need longer for
# generation even with the model loaded, so this is overridable via env.
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "10"))

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


class ManualReviewRequest(BaseModel):
    code_snippet: str


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

def build_review_prompt(code_text: str) -> str:
    """
    Prompt tuned against qwen2.5-coder:1.5b (see also OLLAMA_MODEL). Two
    behaviors specific to this small model drove the shape of this prompt,
    found by direct experimentation against the /api/generate endpoint:

    1. A concrete few-shot example (e.g. a finding about a variable named
       'r') makes the model anchor on that example's literal wording and
       repeat a lookalike finding almost verbatim, even when the actual
       code doesn't have that specific problem. A placeholder example
       ("<bug or security issue>") avoids this while still teaching the
       output format.
    2. Without an explicit checklist of issue categories, the model reports
       only the first issue it notices and stops. The checklist plus
       "report EVERY issue on its own line" is what gets multiple findings
       out, each on a separate line (parse_severity_counts() splits on
       newlines, so findings glued onto one line would otherwise be missed).

    Severity labeling (CRITICAL vs WARNING for the same issue) is still
    inconsistent run-to-run at this model size — that's a model capability
    limit prompting doesn't fully fix, not a parsing bug.
    """
    return (
        "You are an expert automated code reviewer. Analyze the code snippet "
        "for security vulnerabilities, bugs, and code smells. The input may "
        "be a unified diff or a standalone snippet.\n\n"
        "Check specifically for: injection vulnerabilities (SQL/command/etc.), "
        "hardcoded secrets or credentials, missing error handling, "
        "correctness bugs, and naming or style issues. Report EVERY issue "
        "you find, each on its own line, not just the first one.\n\n"
        "Strict Output Rules:\n"
        "- Each finding is exactly one line, separated by a newline "
        "character. Never merge multiple findings onto one line.\n"
        "- Every line MUST start with exactly one prefix: CRITICAL:, "
        "WARNING:, or NITPICK:.\n"
        "- CRITICAL = security vulnerabilities (injection, hardcoded "
        "secrets, auth bypass) or correctness bugs.\n"
        "- WARNING = design or maintainability concerns that are not "
        "security bugs (missing error handling, poor structure).\n"
        "- NITPICK = pure style (naming, formatting, comments).\n"
        "- Do not include conversational intro/outro, repeat the code, or "
        "use markdown.\n\n"
        "Example Output (format only, unrelated to the code below):\n"
        "CRITICAL: <bug or security issue>\n"
        "WARNING: <design or maintainability concern>\n"
        "NITPICK: <style nit>\n\n"
        "Analyze this code:\n"
        f"{code_text}"
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


def _offline_result() -> dict:
    return {
        "review_text": None,
        "critical_count": 0,
        "warning_count": 0,
        "nitpick_count": 0,
        "status": "llm_offline_pending",
    }


async def review_code_with_ollama(code_text: str) -> dict:
    """
    Single entry point for LLM review, shared by the webhook and manual paths.

    Returns:
        {"review_text": str | None, "critical_count": int, "warning_count": int,
         "nitpick_count": int, "status": "reviewed" | "llm_offline_pending"}

    Never raises. Ollama being offline is an expected, routine condition in
    production — not an error — so it's logged at INFO and reported through
    status="llm_offline_pending" with zero counts. review_text stays None in
    that case so offline rows keep storing NULL, as they did before.

    Async (not a plain def) because it performs network I/O from async
    handlers; a blocking client here would stall the event loop.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_review_prompt(code_text),
        "stream": False,
        # Low temperature: at the default (~0.8) this small model's finding
        # count and severity labels vary noticeably between identical
        # requests. Determinism matters more here than creative phrasing.
        "options": {"temperature": 0.2},
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
            response.raise_for_status()
            review_text = response.json().get("response", "")
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        if isinstance(exc, httpx.TimeoutException):
            logger.info(
                "Ollama did not respond within %ss; marking review llm_offline_pending.",
                OLLAMA_TIMEOUT_SECONDS,
            )
        else:
            logger.info("Ollama is unreachable at %s; marking review llm_offline_pending.", OLLAMA_BASE_URL)
        return _offline_result()
    except (httpx.HTTPError, ValueError) as exc:
        # Error status, connection dropped mid-response, or a non-JSON body.
        # Still degrade gracefully: the caller's contract is "never raise".
        logger.warning("Ollama review failed: %s", exc)
        return _offline_result()

    return {"review_text": review_text, **parse_severity_counts(review_text), "status": "reviewed"}


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

        result = await review_code_with_ollama(diff_text)

        review.review_text = result["review_text"]
        review.critical_count = result["critical_count"]
        review.warning_count = result["warning_count"]
        review.nitpick_count = result["nitpick_count"]
        review.status = result["status"]
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


@app.post("/api/reviews/manual", response_model=ReviewOut)
async def manual_review(payload: ManualReviewRequest, db: Session = Depends(get_db)):
    if not payload.code_snippet.strip():
        raise HTTPException(status_code=422, detail="code_snippet cannot be empty")

    result = await review_code_with_ollama(payload.code_snippet)

    new_review = PRReview(
        repo_name="Manual Submission",
        pr_number=0,
        pr_title="N/A",
        pr_url="N/A",
        diff_text=payload.code_snippet,
        review_text=result["review_text"],
        critical_count=result["critical_count"],
        warning_count=result["warning_count"],
        nitpick_count=result["nitpick_count"],
        status=result["status"],
    )
    db.add(new_review)
    db.commit()
    db.refresh(new_review)
    return new_review


@app.get("/api/health", response_model=HealthOut)
async def health():
    ollama_reachable = await check_ollama_reachable()
    return HealthOut(status="ok", ollama_reachable=ollama_reachable)
