# Decisions Log

Every dependency (package, CLI tool, external service) and every significant design choice gets an entry here. New dependencies must be recorded here before they are added (see `.kiro/steering/project.md`, section 6).

Status values: **Accepted** (in use), **Proposed** (not adopted yet, needs owner sign-off), **Rejected**.

## Python runtime dependencies (`requirements.txt`)

| Package | Version | Status | Why | Alternatives considered |
|---|---|---|---|---|
| fastapi | 0.115.0 | Accepted | HTTP framework with request validation via pydantic and async handlers for network-bound work. | Flask (sync by default, no built-in validation); Starlette alone (would mean hand-writing validation). |
| uvicorn[standard] | 0.30.6 | Accepted | ASGI server for FastAPI. The `[standard]` extra adds httptools, watchfiles (`--reload`), and websockets. | Plain `uvicorn` would be enough in production; the extra exists for dev reload. Worth revisiting at deploy time. |
| sqlalchemy | 2.0.35 | Accepted | ORM plus a swap path from SQLite to Postgres through `DATABASE_URL`. | Stdlib `sqlite3` (no backend portability). |
| httpx | 0.27.2 | Accepted | Async HTTP client for Ollama, Judge0, and GitHub diff fetching. A blocking client would stall the event loop. | `requests` (sync only); `aiohttp` (larger API surface). |
| python-dotenv | 1.0.1 | Accepted | Loads `.env` for local development. | Reading `os.environ` only, which makes local setup clunkier. |
| pydantic | 2.13.5 | Accepted | Imported directly by `main.py` for request/response models. Already installed as a FastAPI dependency; pinned explicitly so the version the code was tested with is the one installed. Not a new package. | Leaving it implicit through FastAPI's range (resolves to whatever is newest). |

## Removed dependency

| Package | Status | Notes |
|---|---|---|
| requests | Rejected | Was imported only by `simulate_pr.py` and missing from `requirements.txt`. `simulate_pr.py` now uses `httpx`, which is already a dependency. |

## Frontend dependencies (`frontend/package.json`)

| Package | Version | Status | Why |
|---|---|---|---|
| react, react-dom | 18.3.1 | Accepted | UI library. |
| vite | 6.0.7 | Accepted (dev) | Dev server with `/api` proxy, plus the production build. |
| @vitejs/plugin-react | 4.3.4 | Accepted (dev) | JSX transform and fast refresh for Vite. |
| tailwindcss, @tailwindcss/vite | 4.1.11 | Accepted (dev) | Utility CSS. The v4 Vite plugin removes the need for PostCSS config. |

## External services

| Service | Status | Why | Notes |
|---|---|---|---|
| Ollama (`qwen2.5-coder:1.5b`) | Accepted | Local inference: no API cost, no code leaves the machine. | Treated as unreliable; failures yield `llm_offline_pending`. |
| Judge0 CE public instance | Accepted | Sandboxed Python execution without running user code on the host. | Replaced Piston after Piston's public API became whitelist-only (Feb 2026). Do not revert. |
| Piston | Rejected | Public API now requires whitelisting. Self-hosting needs cgroup v2, which was not available in the dev machine's Docker Desktop VM. | |

## Proposed tooling (not installed)

| Tool | Status | Why | Alternatives |
|---|---|---|---|
| pytest (dev) | Accepted (9.2.0, `requirements-dev.txt`) | Test runner for the pre-commit gate and the pre-push hook. Fixtures (`tmp_path`, `monkeypatch`) give each test its own SQLite file and stubbed Ollama/Judge0 without extra code. | Stdlib `unittest`: zero deps but more boilerplate and no fixture injection. `pytest-asyncio` was not added: async code is driven with `asyncio.run` and FastAPI's `TestClient`, so the stdlib covers it. |
| gitleaks (CLI binary) | Proposed | A real secret scanner for the pre-commit gate. It is a standalone binary, so it adds no Python dependency. | `detect-secrets` (a pip dependency); the regex fallback in the steering file (weak, but zero install). |
| git CLI (for eval) | Proposed | The eval harness fetches fix and parent commits with `git fetch --depth` into a gitignored cache, so diffs are never copied into the repo. No new Python dependency. | GitHub REST API via `httpx` (subject to rate limits without a token); GitHub `.diff` URLs (no reverse diff, no file contents at the parent commit). |

## Python dev/eval dependencies (`requirements-dev.txt`)

| Package | Version | Status | Why | Alternatives considered |
|---|---|---|---|---|
| pytest | 9.2.0 | Accepted | See "Proposed tooling" below. | |
| PyYAML | 6.0.3 | Accepted (dev/eval only) | The owner asked for the eval case file as `eval/cases.yaml`, and the ablation matrix and run configs are YAML too, so labels can carry comments. The stdlib has no YAML parser. PyYAML is already installed as part of `uvicorn[standard]`, so this pins an existing package rather than adding a new one. Only `yaml.safe_load` is used. Not imported by the production app. | JSON/JSON Lines (stdlib, but no comments, which the labeling notes need); TOML via stdlib `tomllib` (read-only and awkward for a list of records); a hand-written YAML subset parser (fragile). |

## Evaluation design decisions

Recorded in full in `docs/EVAL_DESIGN.md`. Summary:

- The eval uses the stdlib plus `httpx` only. Statistics (mean, sample standard deviation, bootstrap) are computed with `statistics` and `random`, not numpy or scipy.
- Bug items are shown to the reviewer as the reverse of the fix (parent state introduced as a change). The reason and its threat to validity are documented in the eval design.
## CI and lint tooling
| Tool | Version | Status | Why | Alternatives considered |
|---|---|---|---|---|
| ruff (dev) | 0.17.3 (`requirements-dev.txt`) | Accepted | Linter run in CI and pre-commit. One binary, fast, configured in `pyproject.toml`. | `flake8` plus plugins (several packages, slower); `pylint` (slow, noisy); stdlib `compileall` (syntax only, no unused-import or undefined-name checks). |
| GitHub Actions (external service) | n/a | Accepted | Runs tests, lint and the frontend build on push and pull request. The repository is already on GitHub, so no extra service is needed. Uses `actions/checkout`, `actions/setup-python`, `actions/setup-node`. | Other CI hosts need separate accounts. |
| pre-commit (dev) | 4.7.0 (`requirements-dev.txt`) | Accepted | Runs ruff and gitleaks on staged changes with one command (`.pre-commit-config.yaml`). Not required for CI, which runs both directly. | A hand-written `.githooks/pre-commit` (the folder is owner-controlled and must not be edited by Kiro); running the two tools by hand, which CONTRIBUTING.md also lists. |
| gitleaks (CLI binary) | 8.30.1 tested | Accepted (was Proposed) | Secret scan on staged changes, used by the pre-commit gate and the pre-commit hook as a `language: system` hook. It is a standalone binary, so no Go toolchain or Python package is added. | `detect-secrets` (pip dependency); the regex fallback in the steering file (weaker). |
| Docker (Dockerfile, Compose) | n/a | Accepted | Packaging for a single-container deployment: one multi-stage image builds the frontend and runs uvicorn as a non-root user. Nothing is added to the app's dependencies. Base images: `node:22-alpine` (build stage only) and `python:3.12-slim`. | A plain `pip install` plus `npm run build` on the host (works, but the build is not reproducible across machines); PaaS buildpacks (tie the deploy to one vendor). |