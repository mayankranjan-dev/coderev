# CodeRev Bot: Repository Audit

Audit date: 2026-10-31. Audited commit: `1b9c3b6` (branch `v2-revamp`, identical to `main`).

## Method

Read every tracked file (`main.py`, `models.py`, `database.py`, `simulate_pr.py`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, all of `frontend/` except `node_modules` and `dist`) plus the untracked `PROJECT_CONTEXT.md`.

Commands run to check claims (output summarized, nothing invented):

| Check | Command | Result |
|---|---|---|
| File sizes | `(Get-Content <file>).Count` | `main.py` 577 lines, `App.jsx` 754, `models.py` 32, `database.py` 35, `simulate_pr.py` 51 |
| Installed packages | `python -m pip freeze` in `.venv` and `venv` | Both venvs exist. Both have `requests` installed locally (2.32.3 and 2.34.2), so `simulate_pr.py` runs here but would fail on a clean `pip install -r requirements.txt`. No `pytest`. |
| Frontend build | `npm run build` in `frontend/` | Succeeds (vite 6.0.7, 27 modules) |
| Prompt contents | Extracted `build_review_prompt` with `ast` and inspected output | Contains concrete example findings (`cursor.execute(...)`, `os.getenv('API_KEY')`, `'user_count'`). Does not contain the `<bug or security issue>` placeholder. Asks for no file or line location. |
| Parser behavior | Same extraction, called `parse_severity_counts` | Counts `CRITICAL: a` and `Critical: a`. Counts zero for `**CRITICAL:** a`, `1. CRITICAL: a`, `- WARNING: a`. |
| Regression origin | `git log -S "cursor.execute"` / `-S "<bug or security issue>"` | Placeholder added in `a689d44`, replaced by concrete examples in `b5aee5f` |
| Local models | `ollama list` | `qwen2.5-coder:1.5b` (ID `d7372fd82851`), `llama3.2:1b`, `all-minilm`. Ollama server was not running, so no review was executed during this audit. |

Not inspected: `.env` (project rule: never touch it). The `.env` drift noted below comes from `PROJECT_CONTEXT.md`, not from my own reading.

## What works

Verified by reading the code unless marked otherwise.

- **Webhook signature check** (`verify_github_signature`). HMAC-SHA256 over the raw body, `hmac.compare_digest`, fails closed when `WEBHOOK_SECRET` is empty, and the raw body is read before JSON parsing. This part is correct.
- **Graceful LLM degradation.** `review_code_with_ollama` catches connect, timeout, HTTP-status, and JSON errors and returns `llm_offline_pending` instead of raising.
- **Judge0 integration.** Errors map to user-facing messages that leak no internal details. Status 1/2 ("still queued") is treated as a failure rather than empty output. Input is capped at `MAX_EXECUTE_CODE_CHARS`. Nothing user-supplied is executed on the host.
- **Frontend.** Builds cleanly (verified). Zero-state Landing is intact: `selectedId` starts `null` and the `selected` memo has no `reviews[0]` fallback. Terminal is dark (`bg-slate-900` / `bg-slate-950`). The frontend finding parser uses the same prefix rule as the backend, so counts and rendering agree.
- **Secrets hygiene in git.** `git ls-files` shows `.env` and `audit.db` are not tracked, and `.gitignore` covers them.

## Problems, in priority order

P0 = blocks the evaluation goal or is a live regression. P1 = must fix before anything is publicly reachable. P2 = correctness and hygiene. P3 = docs accuracy.

### P0-1. The review prompt contains concrete few-shot examples (live regression)

`main.py` lines 221-228 (`build_review_prompt`). The "Example Output" block has three concrete findings: SQL injection with `cursor.execute`, a hardcoded API key with `os.getenv('API_KEY')`, and variable `'x'` renamed to `'user_count'`. The function's own docstring says a placeholder is used to avoid exactly this anchoring problem. Commit `b5aee5f` introduced the regression when it replaced the placeholder from `a689d44`.

Why it matters: this is listed as a forbidden regression. For evaluation it also biases the model toward SQL-injection and secrets findings, which would inflate false positives on control commits and skew per-category recall.

The examples also contradict the rules in the same prompt. The rules say hardcoded secrets are CRITICAL; the example labels one WARNING.

### P0-2. Findings have no file or line location

The prompt never asks for a location, and `parse_severity_counts` only counts prefixes. The eval design defines a catch as "right file and line range", so recall cannot be measured until the output format carries `path:line` and a parser extracts it. This is the main prerequisite for the hero goal.

### P0-3. Large diffs are silently truncated

`review_code_with_ollama` sends the whole diff in one prompt and never sets `num_ctx`, so Ollama's version-dependent default context window applies. Prompts longer than the window are truncated without any error. The code has no size check, no chunking, and does not record `prompt_eval_count` from the response. In an eval, truncation would show up as missed bugs with no explanation.

### P0-4. "Slow" and "offline" are recorded as the same status

`OLLAMA_TIMEOUT_SECONDS` defaults to 10 (line 51). On CPU, a 1.5B model can take longer than that on a real diff, and a timeout is stored as `llm_offline_pending`, the same status as "Ollama not running". The eval needs latency and timeout recorded separately from transport failure. The production status should probably distinguish them too.

### P0-5. No tests, no CI

There is no test suite and no CI workflow. The planned pre-commit gate ("run tests") has nothing to run yet. Two structural issues will make tests harder: `Base.metadata.create_all` runs at import (line 77), so importing `main` creates `audit.db`, and configuration is read into module globals at import.

### P1-1. No authentication or rate limiting

- `POST /api/execute` is an open proxy to the shared Judge0 CE instance. Anyone who can reach the server can run code on Judge0 under your server's identity.
- `POST /api/reviews/manual` has no size cap on `code_snippet` (`/api/execute` has one), so one request can tie up Ollama.
- `GET /api/reviews` returns every row with full `diff_text`, with no pagination or auth. Harmless with public repos today, but it would expose code if private repos were ever connected.

### P1-2. Webhook does all work inside the request

`github_webhook` awaits `process_pull_request_event` (line 509): diff fetch (10 s timeout) plus Ollama (10 s default) before responding. GitHub expects a response within 10 seconds, so slow reviews will be logged as failed deliveries and may be redelivered. Fix direction: respond 202 after inserting the pending row and do the work in a background task.

### P1-3. Duplicate rows on redelivery

The `X-GitHub-Delivery` ID is not stored or checked. Every redelivery (manual or after a timeout from P1-2) creates a new row.

### P1-4. No retry for `llm_offline_pending`

Rows in that status stay there forever. Rows where the diff fetch failed stay `pending` forever (line 464) and the UI cannot tell them apart from in-progress rows.

### P2-1. Exception handling gaps

- `fetch_diff_text` catches only `ConnectError`, `TimeoutException`, and `HTTPStatusError` (line 425). Other `httpx.HTTPError` subclasses such as `ReadError` and `RemoteProtocolError` escape, the handler returns 500, and the row stays `pending`.
- `check_ollama_reachable` has the same narrow catch (line 410), so `/api/health` can 500 instead of reporting `false`.
- `await request.json()` (line 500) on a correctly signed but malformed body raises a 500.
- The diff response body has no size limit.

### P2-2. Parser is strict and quietly lossy

Verified: markdown-decorated (`**CRITICAL:**`) or list-prefixed (`1. CRITICAL:`, `- WARNING:`) lines count zero. The prefix rule `"CRITICAL "` (with a space) also counts lines where the model echoes the rubric back. The prompt defines no explicit "no issues" sentinel, so an empty review cannot be told apart from a format failure. Both points matter for the "parse failure rate" metric.

### P2-3. Nondeterminism is not controlled

`temperature` is 0.2 but no `seed` is set. Run-to-run variance is expected, which is fine as long as the eval records seeds.

### P2-4. Dependency and environment hygiene

- `requests` is imported by `simulate_pr.py` but missing from `requirements.txt`. `httpx` is already a dependency and could replace it.
- `simulate_pr.py` hardcodes a placeholder secret instead of reading `WEBHOOK_SECRET`, so it only works if `.env` uses that exact placeholder.
- `.env` is out of sync with `.env.example` (missing `JUDGE0_*`, per `PROJECT_CONTEXT.md`). Defaults in `main.py` cover the gap. Per project rules, the fix is for the owner to apply by hand.
- Two virtualenvs (`.venv`, `venv`) with different `requests` versions.
- Unused import `timezone` in `main.py` (line 28). Pydantic v1-style `class Config` (line 100) works under pydantic 2.13.5 but is deprecated.

### P2-5. Frontend build not served by the backend

`frontend/dist` builds, but FastAPI never mounts it. Production currently means running the Vite dev server. No deployment docs or config exist, although the `main.py` header mentions Render.

### P3-1. README accuracy

- Line counts are stale (README: `main.py` 670, `App.jsx` 729; actual 577 and 754). The line-range map of `main.py` is stale too.
- "Live Demo" and "Documentation" links point to `#`. `assets/project-demo.gif` does not exist, and `assets/` is untracked.
- Capability claims ("Detects SQL injection, command injection, hardcoded secrets...") are unmeasured. Under the no-invented-metrics rule they should become measured claims linked to eval runs, or be softened.
- The troubleshooting section says to avoid the walrus operator on Python 3.8. The walrus operator was added in 3.8, so that advice is wrong.
- The example snippet contains a fake key string that secret scanners may flag.

### P3-2. Untracked files

`PROJECT_CONTEXT.md` and `assets/` are untracked. They were left as-is; the owner decides whether to commit them.

## Recommended order of work

1. Restore the placeholder-only prompt example (P0-1) and add a location format plus a "no issues" sentinel to the prompt and parser (P0-2, P2-2). Keep "Correction:" on the same line.
2. Add a pytest suite for the pure functions (signature check, parser, Judge0 stderr folding) and a CI workflow (P0-5).
3. Build the eval harness per `docs/EVAL_DESIGN.md`, recording `prompt_eval_count`, latency, and timeouts (P0-3, P0-4).
4. Move webhook work to the background, dedupe deliveries, and add a retry path (P1-2, P1-3, P1-4).
5. Add an API key and rate limits on `/api/execute` and `/api/reviews/manual`, plus a size cap on manual reviews (P1-1).
6. Clean up hygiene and docs (P2, P3). Serve `dist/` from FastAPI and write deployment docs.
