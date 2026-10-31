# Known Limitations — SQLite on Render free tier is ephemeral, so the retry
# queue (which lives in the same table) is lost on restart there.
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
- Ollama is treated as an unreliable, optional dependency: an unreachable
  server gives "llm_offline_pending", a slow one "llm_timeout", and an HTTP or
  body error "llm_error", rather than a request failure. retry_queue.py
  retries those rows with backoff. This is the core reliability requirement
  of the service — a dead LLM must never crash the webhook handler or lose
  the PR.
- The webhook handler only verifies, dedupes, and inserts a pending row; the
  diff fetch and review run as a background task after the response.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import database
import retry_queue
from database import SessionLocal, get_db
from models import PRReview
from frontend_static import FrontendApp, frontend_dist_dir
from reviewer import engine
from reviewer.engine import EngineConfig, counts, run_review
from reviewer.findings import Finding, parse_review
from reviewer.prompts import build_review_prompt
from reviewer.providers import OllamaProvider, ReviewProvider
from security import cors_origins_from_env, rate_limit, require_api_key

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("coderev_bot")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
# Default 10s per the original spec. CPU-only machines can need longer for
# generation even with the model loaded, so this is overridable via env.
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "10"))
# Context window sent with every request. Always explicit: Ollama's default
# depends on the version and model, and it truncates longer prompts silently.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# "whole" sends the diff in one request; "per_file" sends one request per
# changed file. Either way input over the num_ctx budget is cut at a hunk
# boundary and the cut is logged and stored (reviewer/diffutil.py).
REVIEW_CHUNKING = os.getenv("REVIEW_CHUNKING", "whole")
# Extra attempts when the model's output is blank or has no parseable line.
REVIEW_MAX_RETRIES = int(os.getenv("REVIEW_MAX_RETRIES", "1"))

# Judge0 Community Edition runs submitted code in an isolated worker with its
# own CPU/memory/process limits. Execution is deliberately delegated to it
# rather than run in-process: nothing the user pastes is ever exec()'d,
# imported, or written to disk on this host.
#
# `wait=true` makes this a synchronous call — Judge0 holds the connection until
# the run finishes and returns the result inline, so there's no token to poll.
# `base64_encoded=false` means source_code goes over as plain UTF-8 text.
JUDGE0_API_URL = os.getenv(
    "JUDGE0_API_URL", "https://ce.judge0.com/submissions?base64_encoded=false&wait=true"
)
# 71 is Python 3 in Judge0 CE's language table (GET /languages).
JUDGE0_PYTHON_LANGUAGE_ID = int(os.getenv("JUDGE0_PYTHON_LANGUAGE_ID", "71"))
# Generous relative to the ~1s typical turnaround: the free instance queues
# submissions under load, and `wait=true` keeps the socket open for the wait.
JUDGE0_TIMEOUT_SECONDS = float(os.getenv("JUDGE0_TIMEOUT_SECONDS", "30"))
# Judge0's "Accepted" status. Any other status means the program didn't finish
# cleanly, which matters when it produced no output to explain why.
JUDGE0_STATUS_ACCEPTED = 3
# Guards against shipping a multi-megabyte paste to a shared free service.
MAX_EXECUTE_CODE_CHARS = int(os.getenv("MAX_EXECUTE_CODE_CHARS", "50000"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Tables are created at startup, not at import, so importing this module
    # (tests, scripts) never creates audit.db as a side effect.
    database.init_db()

    stop = asyncio.Event()
    worker = None
    if retry_queue.worker_enabled():
        # Lambdas look the functions up at call time, so stubs patched onto
        # this module (tests) are what the worker calls.
        worker = asyncio.create_task(
            retry_queue.worker_loop(
                lambda code: review_code_with_ollama(code),
                lambda: check_ollama_reachable(),
                stop,
            )
        )
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            await worker


app = FastAPI(title="CodeRev Bot", version="1.0.0", lifespan=lifespan)

# Same-origin requests (the built frontend, or the Vite proxy in dev) need no
# CORS at all. This only admits the listed cross-origin callers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_from_env(),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Rate limit runs before the API key check so key-guessing is throttled too.
execute_rate_limit = rate_limit("execute", "RATE_LIMIT_EXECUTE_PER_MINUTE", 10)
manual_review_rate_limit = rate_limit("manual_review", "RATE_LIMIT_REVIEW_PER_MINUTE", 5)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class FindingOut(BaseModel):
    severity: str
    file: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    message: str
    correction: Optional[str] = None


def findings_for(review: PRReview) -> list[FindingOut]:
    """
    Stored findings if present; otherwise parse review_text, so rows written
    before findings_json existed still render as structured findings.
    """
    raw = None
    if review.findings_json:
        try:
            raw = json.loads(review.findings_json)
        except ValueError:
            raw = None
    if isinstance(raw, list):
        findings = [Finding.from_dict(item) for item in raw if isinstance(item, dict)]
    else:
        findings = parse_review(review.review_text or "").findings
    return [FindingOut(**finding.to_dict()) for finding in findings]


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
    truncated: bool = False
    findings: list[FindingOut] = []

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_row(cls, review: PRReview) -> "ReviewOut":
        out = cls.model_validate(review)
        out.findings = findings_for(review)
        return out


class HealthOut(BaseModel):
    status: str
    ollama_reachable: bool


class ManualReviewRequest(BaseModel):
    code_snippet: str


class ExecuteRequest(BaseModel):
    code: str


class ExecuteOut(BaseModel):
    stdout: str
    stderr: str


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

class SandboxError(RuntimeError):
    """
    Raised when the sandbox itself fails (unreachable, timed out, rate limited,
    malformed response) — never for user code that merely exits non-zero.

    Provider-neutral on purpose: the execution backend has already been swapped
    once (Piston -> Judge0), and the endpoint's error contract shouldn't churn
    with it.

    The message is user-facing: it is surfaced in the terminal's stderr pane, so
    it must stay free of internal URLs, stack detail, or host information.
    """


# engine.Failure.kind -> stored status. The first three are retried later by
# retry_queue.py. Malformed/empty output was already retried by the engine
# (REVIEW_MAX_RETRIES), so those are final and keep the raw text for display.
FAILURE_STATUS = {
    engine.UNREACHABLE: "llm_offline_pending",
    engine.TIMEOUT: "llm_timeout",
    engine.PROVIDER_ERROR: "llm_error",
    engine.MALFORMED_OUTPUT: "llm_malformed_output",
    engine.EMPTY_OUTPUT: "llm_empty_output",
}


def get_review_provider() -> ReviewProvider:
    """
    The provider used by the production paths. Built per call from module
    config (cheap: no connection is opened until generate()).
    """
    return OllamaProvider(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        timeout_s=OLLAMA_TIMEOUT_SECONDS,
        num_ctx=OLLAMA_NUM_CTX,
        # Low temperature: at the default (~0.8) this small model's finding
        # count and severity labels vary noticeably between identical
        # requests. Determinism matters more here than creative phrasing.
        temperature=0.2,
    )


def _failed_result(status: str, error: str, review_text: Optional[str] = None) -> dict:
    """
    `error` is stored in last_error, so it names the failure class only,
    never URLs or hosts.
    """
    return {
        "review_text": review_text,
        "critical_count": 0,
        "warning_count": 0,
        "nitpick_count": 0,
        "findings": [],
        "status": status,
        "error": error,
    }


async def review_code_with_ollama(code_text: str) -> dict:
    """
    Single entry point for LLM review, shared by the webhook and manual paths.

    Returns:
        {"review_text": str | None, "critical_count": int, "warning_count": int,
         "nitpick_count": int, "findings": list, "truncated": bool,
         "status": "reviewed" | one of FAILURE_STATUS values,
         "error": str | None}

    Never raises. Ollama being offline is an expected, routine condition in
    production — not an error — so it's logged at INFO and reported through
    status="llm_offline_pending" with zero counts. review_text is None for
    transport failures and holds the raw model text for output failures.

    Async (not a plain def) because it performs network I/O from async
    handlers; a blocking client here would stall the event loop.
    """
    provider = get_review_provider()
    config = EngineConfig(chunking=REVIEW_CHUNKING, max_retries=REVIEW_MAX_RETRIES)
    result = await run_review(code_text, provider, build_review_prompt, config)

    failure = result.failure
    if failure is not None:
        status = FAILURE_STATUS.get(failure.kind, "llm_error")
        # Unreachable is routine (Ollama not started), so INFO; others WARNING.
        log = logger.info if failure.kind == engine.UNREACHABLE else logger.warning
        log("Review failed (%s) after %s retr(y/ies); marking %s.", failure, result.retries, status)
        raw = result.raw_text if failure.kind not in engine.TRANSPORT_KINDS else ""
        raw = raw if raw.strip() else None
        return _failed_result(status, str(failure), raw) | {"truncated": result.truncated}

    if result.truncated:
        logger.warning("Review input was truncated to fit the context window: %s", result.plan.to_dict())
    if result.parse_failures:
        logger.info("Review output had %s unparseable line(s).", len(result.parse_failures))

    return {
        "review_text": result.raw_text,
        **counts(result.findings),
        "findings": [finding.to_dict() for finding in result.findings],
        "truncated": result.truncated,
        "status": "reviewed",
        "error": None,
    }


def _combine_judge0_stderr(body: dict) -> str:
    """
    Fold Judge0's several failure channels into one stderr string.

    Judge0 splits diagnostics across three fields, any of which may be JSON
    null, and which one is populated depends on how the program died:
      - compile_output: rejected before running (for Python: SyntaxError)
      - stderr:         ran, then raised (traceback)
      - message:        harness-level note, e.g. the reason for a kill

    compile_output is placed first because compilation precedes execution, so
    that ordering matches the order the failures actually happened in.

    A non-Accepted status with nothing captured (time limit exceeded is the
    common case) would otherwise render as an empty terminal, so the status
    description is used as the last-resort explanation.
    """
    status = body.get("status") or {}
    status_id = status.get("id")
    status_description = (status.get("description") or "").strip()

    segments = []
    for field in ("compile_output", "stderr", "message"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            segments.append(value.strip("\n"))

    if segments:
        return "\n".join(segments)

    # Nothing captured. Only explain ourselves if the run wasn't clean.
    if status_id != JUDGE0_STATUS_ACCEPTED and status_description:
        return status_description

    return ""


async def execute_code_with_judge0(code: str) -> dict:
    """
    Run `code` on the Judge0 sandbox and return {"stdout": str, "stderr": str}.

    Raises SandboxError on transport/protocol failure so the endpoint can map it
    to a 500. A *program* that fails (syntax error, exception, non-zero exit) is
    not a failure of this function — Judge0 reports that in stderr /
    compile_output, which is passed through so the user sees their own traceback.
    """
    payload = {
        "source_code": code,
        "language_id": JUDGE0_PYTHON_LANGUAGE_ID,
    }

    try:
        async with httpx.AsyncClient(timeout=JUDGE0_TIMEOUT_SECONDS) as client:
            response = await client.post(JUDGE0_API_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        logger.warning("Judge0 execution timed out after %ss: %s", JUDGE0_TIMEOUT_SECONDS, exc)
        raise SandboxError(
            f"Execution timed out after {JUDGE0_TIMEOUT_SECONDS:g}s. "
            "The sandbox did not respond in time."
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("Judge0 returned HTTP %s: %s", status, exc.response.text[:500])
        # 429 is the most likely failure on the shared free instance; 401/403
        # would mean the public CE endpoint has started requiring a key.
        if status == 429:
            detail = "Sandbox rate limit reached. Wait a moment and try again."
        elif status in (401, 403):
            detail = "Sandbox rejected the request as unauthorized."
        else:
            detail = f"Sandbox returned an error (HTTP {status})."
        raise SandboxError(detail) from exc
    except httpx.HTTPError as exc:
        logger.warning("Judge0 request failed: %s", exc)
        raise SandboxError("Could not reach the execution sandbox.") from exc
    except ValueError as exc:
        logger.warning("Judge0 returned a non-JSON body: %s", exc)
        raise SandboxError("Sandbox returned a malformed response.") from exc

    # A non-dict body means the contract changed — treat as an error rather than
    # silently reporting empty output as a successful run.
    if not isinstance(body, dict):
        logger.warning("Judge0 response was not a JSON object: %s", str(body)[:500])
        raise SandboxError("Sandbox returned an unexpected response shape.")

    # With wait=true a finished submission always carries a status. Still queued
    # (1) or processing (2) means wait=true didn't hold, and returning the empty
    # result as-is would look like a program that printed nothing.
    status_id = (body.get("status") or {}).get("id")
    if status_id in (1, 2):
        logger.warning("Judge0 returned an unfinished submission: status id %s", status_id)
        raise SandboxError("Sandbox did not finish the run in time.")

    return {"stdout": body.get("stdout") or "", "stderr": _combine_judge0_stderr(body)}


async def check_ollama_reachable() -> bool:
    """Real connectivity check against Ollama's /api/tags endpoint."""
    return await get_review_provider().is_available()


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
    except httpx.HTTPError as exc:
        # Any transport or status failure (including ReadError and
        # RemoteProtocolError) degrades to "no diff" instead of escaping.
        logger.warning("Failed to fetch diff from %s: %s", diff_url, exc)
        return None


def create_pending_review(payload: dict) -> Optional[int]:
    """
    Insert the pending row for a pull_request event and return its id.

    Returns None when a row for the same (repo, PR number, head SHA) already
    exists. The unique index on those columns is what enforces this, so two
    concurrent redeliveries cannot both insert.
    """
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}

    db: Session = SessionLocal()
    try:
        review = PRReview(
            repo_name=repository.get("full_name", "unknown/unknown"),
            pr_number=pull_request.get("number", payload.get("number", 0)),
            pr_title=pull_request.get("title", ""),
            pr_url=pull_request.get("html_url", ""),
            head_sha=(pull_request.get("head") or {}).get("sha"),
            diff_text=None,
            review_text=None,
            status="pending",
        )
        db.add(review)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        return review.id
    finally:
        db.close()


async def process_review_job(review_id: int, diff_url: str) -> None:
    """
    Background half of the webhook: fetch the diff and run the review, then
    update the row. Runs after the HTTP response has been sent, so a slow diff
    fetch or model call never pushes the webhook past GitHub's timeout.
    Never raises: a background exception would only be logged by Starlette and
    leave the row stuck in "pending".
    """
    db: Session = SessionLocal()
    try:
        review = db.get(PRReview, review_id)
        if review is None:
            return

        diff_text = await fetch_diff_text(diff_url) if diff_url else None
        review.diff_text = diff_text
        if not diff_text:
            logger.warning(
                "No diff text available for PR #%s in %s.", review.pr_number, review.repo_name
            )
            review.status = "diff_unavailable"
            db.commit()
            return
        db.commit()

        result = await review_code_with_ollama(diff_text)
        retry_queue.apply_review_result(review, result)
        db.commit()
    except Exception:  # noqa: BLE001  (last-resort guard for a background task)
        logger.exception("Review job %s failed unexpectedly.", review_id)
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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

    try:
        payload = json.loads(raw_body)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Body is not valid JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be a JSON object"})

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "not a pull_request event"}

    action = payload.get("action")
    if action not in ("opened", "synchronize"):
        return {"status": "ignored", "reason": f"action '{action}' not handled"}

    pull_request = payload.get("pull_request") or {}
    if not (pull_request.get("head") or {}).get("sha"):
        return JSONResponse(status_code=400, content={"detail": "pull_request.head.sha is missing"})

    review_id = create_pending_review(payload)
    if review_id is None:
        # Redelivery of an event already recorded for this head commit.
        return {"status": "duplicate"}

    background_tasks.add_task(process_review_job, review_id, pull_request.get("diff_url", ""))
    return JSONResponse(status_code=202, content={"status": "accepted", "review_id": review_id})


@app.get("/api/reviews", response_model=list[ReviewOut], dependencies=[Depends(require_api_key)])
def get_reviews(db: Session = Depends(get_db)):
    reviews = db.query(PRReview).order_by(PRReview.created_at.desc()).all()
    return [ReviewOut.from_row(review) for review in reviews]


@app.post(
    "/api/reviews/manual",
    response_model=ReviewOut,
    dependencies=[Depends(manual_review_rate_limit), Depends(require_api_key)],
)
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
        attempt_count=0,
    )
    retry_queue.apply_review_result(new_review, result)
    db.add(new_review)
    db.commit()
    db.refresh(new_review)
    return ReviewOut.from_row(new_review)


@app.post(
    "/api/execute",
    response_model=ExecuteOut,
    dependencies=[Depends(execute_rate_limit), Depends(require_api_key)],
)
async def execute_code(payload: ExecuteRequest):
    """
    Run a Python snippet in the Judge0 sandbox and return its captured output.

    The response shape is {"stdout", "stderr"} on both the success and failure
    paths — on failure the status is 500 and the reason is placed in stderr, so
    the frontend terminal can render every outcome through one code path.
    """
    if not payload.code.strip():
        return ExecuteOut(stdout="", stderr="No code to execute.")

    if len(payload.code) > MAX_EXECUTE_CODE_CHARS:
        return JSONResponse(
            status_code=413,
            content={
                "stdout": "",
                "stderr": f"Code exceeds the {MAX_EXECUTE_CODE_CHARS} character execution limit.",
            },
        )

    try:
        result = await execute_code_with_judge0(payload.code)
    except SandboxError as exc:
        return JSONResponse(status_code=500, content={"stdout": "", "stderr": str(exc)})

    return ExecuteOut(**result)


@app.get("/api/health", response_model=HealthOut)
async def health():
    ollama_reachable = await check_ollama_reachable()
    return HealthOut(status="ok", ollama_reachable=ollama_reachable)


# ---------------------------------------------------------------------------
# Built frontend. Keep this LAST: a mount at "/" matches every path, so any
# route registered after it would be shadowed.
# ---------------------------------------------------------------------------

frontend_app = FrontendApp(frontend_dist_dir())
app.mount("/", frontend_app, name="frontend")
