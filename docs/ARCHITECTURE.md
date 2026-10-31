# Architecture

How CodeRev Bot is put together and why. File and function names are from the code at the time of writing; the audit trail of what changed is in `docs/AUDIT.md`.

## Components

| Component | File | Role |
|---|---|---|
| HTTP API | `main.py` | Endpoints, webhook verification, diff fetching, Judge0 client, wiring of the engine to the database. |
| Access control | `security.py` | `X-API-Key` check (fails closed), in-memory sliding-window rate limiter, CORS origin list. |
| Review engine | `reviewer/engine.py` | Chunk, prompt, call the provider, parse, bounded retry on unusable output, merge. Never raises. |
| Provider interface | `reviewer/providers.py` | `ReviewProvider` (abstract), `OllamaProvider`, `StubProvider` for tests and the eval gate. |
| Diff handling | `reviewer/diffutil.py` | Unified diff parsing, new-file line mapping, chunking to a character budget. |
| Prompts | `reviewer/prompts.py`, `prompts/*.txt` | Versioned prompt files, placeholder-only format examples. |
| Parser | `reviewer/findings.py` | Tolerant parser to structured findings (severity, path, lines, text, correction) plus parse failures. |
| Retry queue | `retry_queue.py` | Background worker that retries failed reviews with backoff. The queue is the `pr_reviews` table. |
| Storage | `models.py`, `database.py` | SQLAlchemy model and session; SQLite by default. |
| Frontend | `frontend/src/App.jsx`, `frontend_static.py` | React UI; the built files are served by the backend at `/`. |
| Eval harness | `eval/` | Case loader, fetcher, run harness, report, ablation runner, author-owned matcher. |

## Data flow

### Webhook review

1. GitHub posts a `pull_request` event. The handler reads the raw body and verifies `X-Hub-Signature-256` (HMAC-SHA256, constant-time compare) before parsing JSON.
2. Only `opened` and `synchronize` are processed. The handler inserts a `pending` row keyed on (repo, PR number, head SHA). A redelivery for the same key returns `duplicate` and creates no row.
3. It answers `202` and schedules `process_review_job` as a background task. This keeps the response well inside GitHub's delivery window.
4. The job fetches the diff from the PR's `diff_url`, streaming with a byte limit (`MAX_DIFF_BYTES`). Above the limit the row becomes `diff_too_large`; if the fetch fails it becomes `diff_unavailable`. Neither stays `pending`.
5. The job calls the review engine and records the result with `retry_queue.apply_review_result`.

### Manual review

`POST /api/reviews/manual` (API key, rate limit, size limit) calls the same engine on the snippet and stores the row. The response is the full review including structured findings. If the model is down, the row is still stored, with a retryable status, and the retry worker picks it up later.

### Sandbox execution

`POST /api/execute` (API key, rate limit, size limit) sends `{"source_code", "language_id"}` to Judge0 CE with `wait=true`. Judge0 status codes and errors are folded into a `{stdout, stderr}` response so the terminal has one rendering path. Nothing user-submitted runs on the host.

### Review engine

1. `diffutil` splits the input to a character budget derived from `OLLAMA_NUM_CTX`: the whole diff, or one chunk per file (`REVIEW_CHUNKING`). If a chunk is over budget it is cut at a hunk or line boundary and the cut is recorded (`truncated`, omitted files and hunks). Nothing is dropped silently.
2. The prompt is built from `prompts/<version>.txt`.
3. The provider returns text or raises `ProviderError`.
4. `findings.parse_review` returns structured findings. Output with neither a valid finding line nor the `NO_ISSUES_FOUND` sentinel is `malformed_output`; blank output is `empty_output`.
5. Unusable output is retried up to `REVIEW_MAX_RETRIES` extra times, with the next seed when a seed is set. Only the last attempt counts; all attempts are kept.
6. Per-chunk results are merged into one `ReviewResult`.

The production paths and the eval harness call this same engine and parser, so the eval measures the shipped code path.

## Failure handling

### Typed failures

The engine reports one of five kinds. Production maps each to a row status.

| Kind | Meaning | Row status | Retried by |
|---|---|---|---|
| `unreachable` | Connection refused or connect timeout | `llm_offline_pending` | Retry queue |
| `timeout` | Connected, but no answer within `OLLAMA_TIMEOUT_SECONDS` | `llm_timeout` | Retry queue |
| `provider_error` | HTTP error status or an unusable response body | `llm_error` | Retry queue |
| `malformed_output` | Text with no valid finding and no sentinel | `llm_malformed_output` | Engine, bounded, immediately |
| `empty_output` | Blank text | `llm_empty_output` | Engine, bounded, immediately |

Other terminal statuses: `reviewed`, `diff_too_large`, `diff_unavailable`, and `llm_failed` (retry attempts exhausted).

### Timeouts versus offline

A slow model and a dead model need different handling. A CPU-only machine can take longer than the default timeout on a real diff. Treating that as "offline" hides the cause and makes latency look like an outage. So `unreachable` (connect failure) and `timeout` (connected, slow) are separate statuses, and the eval records latency and a "would have timed out in production" rate.

### Retry queue

- Rows in `llm_offline_pending`, `llm_timeout` or `llm_error` are due when `next_attempt_at` is empty or past.
- The worker first checks that the provider is reachable, so attempts are not burned while Ollama is down.
- Each attempt increments `attempt_count` and stores `last_error` (a failure class name, not raw model text).
- Backoff doubles from `RETRY_BACKOFF_BASE_SECONDS` up to `RETRY_BACKOFF_MAX_SECONDS`.
- After `RETRY_MAX_ATTEMPTS` the row becomes `llm_failed` and is left alone.
- The worker never raises; an exception in one retry is recorded as `llm_error` for that row.
- `malformed_output` and `empty_output` rows are not requeued: the same prompt was already retried in the engine.

The queue lives in the same SQLite table as the reviews. On a host that wipes its disk, the queue is lost with the reviews.

## Design trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Local 1.5B model through Ollama | No API cost, code stays on the machine | Weak recall is likely; slow on CPU |
| Provider interface | Tests and the eval gate run without a model; another backend can be added | One more layer to read |
| Structured findings (with locations) | Catches can be scored by file and line; the UI can render them | The model must follow a stricter format, so parse failures exist and are measured |
| Tolerant parser | Markdown or list decoration does not zero a review | Can accept lines the model did not mean as findings |
| Background webhook job | Fast `202`, no redelivery storms | Work is lost if the process dies mid-job (the row stays `pending`) |
| Queue in the reviews table | No new service | Same durability as the database; polling instead of push |
| Dedupe on (repo, PR, head SHA) | Simple and stable | Does not use `X-GitHub-Delivery`; a new push creates a new row by design |
| Shared API key | Small surface, easy to rotate | No per-user identity or audit |
| In-memory rate limiter | No dependency | Per process, resets on restart, does not trust `X-Forwarded-For` |
| Public Judge0 CE | No sandbox to run | Shared quota, no SLA, Python 3.8, code leaves the machine |
| SQLite | Zero setup | Single writer, ephemeral on free hosts; only SQLite has been tested |
| Prompts with placeholder-only examples | No anchoring on a particular bug type | Weaker format guidance for a small model |
