# Deployment Guide

How to run CodeRev Bot locally and in Docker. Each command below is marked with how far it was checked. "Checked" means a command was run in this repo on 2026-10-31. Anything else is marked "not run".

## What runs where

- **API + UI**: one FastAPI process (`main:app`). It serves the API under `/api` and the built frontend at `/`.
- **Ollama**: a separate process on the host. The app treats it as optional. When it is down, reviews are stored as `llm_offline_pending` and retried (`retry_queue.py`).
- **Judge0 CE**: an external HTTP service for the code sandbox. Nothing user-submitted runs on the host.
- **SQLite**: a single file (`audit.db`, or `/data/audit.db` in Docker).

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # then edit .env; see the variables below
ollama pull qwen2.5-coder:1.5b
```

Backend, in its own terminal (developer mode, auto-reload):

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Frontend, either as the dev server (proxies `/api` to port 8000) or built and served by the backend:

```powershell
cd frontend
npm ci
npm run dev        # http://localhost:5173
# or
npm run build      # then open http://127.0.0.1:8000, served by the backend from frontend/dist
```

Check it: `http://127.0.0.1:8000/api/health` returns `{"status":"ok","ollama_reachable":true|false}`.

Status of these commands:

- Checked: `npm run build` exits 0. The backend imports and answers `GET /api/health` with 200 and `GET /` with 200 from a clean directory holding only the files the Docker image ships (through FastAPI's `TestClient`, with Ollama unreachable, so `ollama_reachable` was `false`).
- Not run: the `uvicorn` command itself as a live server, `npm run dev`, and `ollama pull`. Long-running processes are started by the owner.

## Run with Docker

```powershell
docker compose up --build
# in another terminal:
curl http://127.0.0.1:8000/api/health
```

`docker-compose.yml` builds the image (stage 1: Node builds the frontend; stage 2: Python runs `uvicorn` as the non-root user `coderev`), mounts the named volume `coderev-data` at `/data` for the SQLite file, and passes settings from your shell environment or from a `.env` file next to the compose file (Compose uses it for `${...}` substitution only; nothing is baked into the image).

```powershell
$env:WEBHOOK_SECRET = "<your webhook secret>"
$env:CODEREV_API_KEY = "<your API key>"
docker compose up --build -d
docker compose logs -f coderev       # follow logs
docker compose down                  # stop; the volume and database stay
docker compose down -v               # stop AND delete the database volume
```

Plain `docker` without Compose:

```powershell
docker build -t coderev-bot:local .
docker run --rm -p 8000:8000 -v coderev-data:/data `
  -e WEBHOOK_SECRET -e CODEREV_API_KEY `
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 `
  coderev-bot:local
```

Status: **not run.** Docker is installed on the development machine but the Docker daemon was not running on 2026-10-31 (`docker version` could not reach `dockerDesktopLinuxEngine`), so the image was never built here and the commands above have not been executed. The first `docker compose up --build` is the verification step. The image has a `HEALTHCHECK` on `/api/health`, so `docker compose ps` should show `healthy` once it is up.

## Environment variables

`.env.example` is the reference. Compare your `.env` to it by name, without printing any value:

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
```

### Required

| Variable | Purpose |
|---|---|
| `WEBHOOK_SECRET` | HMAC secret shared with the GitHub webhook. Empty means every webhook is rejected. |
| `CODEREV_API_KEY` | Value clients send in `X-API-Key` for `/api/reviews`, `/api/reviews/manual` and `/api/execute`. Empty means those endpoints refuse every request (403). |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./audit.db` | SQLAlchemy URL. Docker sets `sqlite:////data/audit.db`. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama listens. Compose defaults to `http://host.docker.internal:11434`. |
| `OLLAMA_MODEL` | `qwen2.5-coder:1.5b` | Model used for reviews. |
| `OLLAMA_TIMEOUT_SECONDS` | `10` | Per-request timeout. CPU-only machines often need more. |
| `OLLAMA_NUM_CTX` | `8192` | Context window sent on every request. |
| `REVIEW_CHUNKING` | `whole` | `whole` or `per_file`. |
| `REVIEW_MAX_RETRIES` | `1` | Extra attempts after blank or unparseable output. |
| `REVIEW_PROMPT_VERSION` | `v1` | Which `prompts/<version>.txt` production uses. |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Cross-origin callers. Same-origin use needs none. |
| `FRONTEND_DIST_DIR` | `frontend/dist` | Built frontend location. |
| `RATE_LIMIT_EXECUTE_PER_MINUTE` | `10` | Per-IP limit on `/api/execute`. `0` disables. |
| `RATE_LIMIT_REVIEW_PER_MINUTE` | `5` | Per-IP limit on `/api/reviews/manual`. `0` disables. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Window for the two limits above. |
| `JUDGE0_API_URL` | `https://ce.judge0.com/submissions?base64_encoded=false&wait=true` | Sandbox endpoint. Keep both query parameters. |
| `JUDGE0_PYTHON_LANGUAGE_ID` | `71` | Python 3 in Judge0 CE. |
| `JUDGE0_TIMEOUT_SECONDS` | `30` | Sandbox request timeout. |
| `MAX_EXECUTE_CODE_CHARS` | `50000` | Largest snippet `/api/execute` accepts (413 above). |
| `MAX_REVIEW_CODE_CHARS` | `100000` | Largest manual review submission (413 above). |
| `MAX_DIFF_BYTES` | `2000000` | Largest PR diff fetched; larger ones get status `diff_too_large`. |
| `REVIEWS_DEFAULT_LIMIT` | `50` | Page size of `GET /api/reviews` when `limit` is omitted. |
| `REVIEWS_MAX_LIMIT` | `200` | Largest allowed `limit` (422 above). |
| `RETRY_WORKER_ENABLED` | `1` | Background retry of offline/timed-out reviews. |
| `RETRY_POLL_SECONDS` | `30` | How often the worker looks for due rows. |
| `RETRY_MAX_ATTEMPTS` | `5` | Attempts before a row becomes `llm_failed`. |
| `RETRY_BACKOFF_BASE_SECONDS` | `60` | First retry delay; it grows from here. |
| `RETRY_BACKOFF_MAX_SECONDS` | `3600` | Cap on the retry delay. |
| `PORT` | `8000` | Compose only: host port mapped to the container's 8000. |

Tool-only variable (not read by the app): `GITHUB_TOKEN`, an optional token for `eval/suggest.py` (see `eval/LABELING.md`). Set it in your shell, not in `.env`.

## Warning: SQLite on a free host is ephemeral

Free tiers of container hosts (Render's free web service is the one named in `main.py`) give the container a disk that is wiped on every restart, redeploy, and spin-down. With the default `DATABASE_URL`:

- every stored review is lost,
- the retry queue is lost (it is the same table),
- webhook dedupe forgets earlier deliveries, so a redelivery after a restart creates a new row.

Fixes: attach a persistent disk and point `DATABASE_URL` at it (on Docker, that is the `coderev-data` volume), or treat the deployment as a demo and accept the loss. A hosted database would need a driver and a migration check that have not been done; the app has only been run on SQLite.

## Exposing Ollama

Ollama has no authentication. Anyone who can reach its port can run any model you have pulled, at your cost.

- **Same machine as the app (no Docker)**: keep the default, listening on `127.0.0.1:11434`. Do not expose it.
- **App in Docker, Ollama on the host**: the container reaches the host at `host.docker.internal`. Docker Desktop (Windows, macOS) generally allows this while Ollama stays on localhost; on Linux Ollama must listen on an address the container can reach (for example `OLLAMA_HOST=0.0.0.0` or the Docker bridge address). Not run here.
- **App on a remote host, Ollama at home**: do not forward port 11434 to the internet. Put it behind a private network (a VPN or a tunnel with authentication) or a reverse proxy that requires a credential, and set `OLLAMA_BASE_URL` to that address. Note that a free host's reviews will then depend on your machine being on, and a slow CPU model may exceed `OLLAMA_TIMEOUT_SECONDS`; the app then stores `llm_timeout` and retries.
- After any change to who can reach Ollama, test with `/api/health`: `ollama_reachable` must be `true`.

## Webhook setup

Point a GitHub webhook at `https://<your-host>/api/webhook/github`, content type `application/json`, secret equal to `WEBHOOK_SECRET`, event "Pull requests". Only `opened` and `synchronize` actions are processed. The endpoint answers `202` and does the work in the background. GitHub must be able to reach the host, so a local machine needs a public tunnel.
