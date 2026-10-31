# v2: structured reviewer, hardened service, evaluation harness

Branch `v2-revamp` into `main`. Prepared text only; the owner opens and merges the pull request.

## Summary

### 1. Reviewer engine rewrite

- Review provider interface (`reviewer/providers.py`) with an Ollama implementation and a stub for tests.
- Structured findings with file and line locations and a tolerant parser, replacing severity-line counting. An explicit `NO_ISSUES_FOUND` sentinel separates a clean review from a format failure.
- Diff line mapping and chunking (whole diff or per file) to a budget derived from `num_ctx`. Cuts are recorded, not silent.
- Typed failures (`unreachable`, `timeout`, `provider_error`, `malformed_output`, `empty_output`) with a bounded retry for unusable output.
- Versioned prompts under `prompts/`, placeholder-only format examples (the concrete few-shot regression is removed).
- Frontend adapted to structured findings.

### 2. Service hardening and delivery

- API key on review and execute endpoints (fails closed), per-IP rate limits, size limits, CORS allow-list, pagination on `GET /api/reviews`.
- Webhook: HMAC check, dedupe on (repo, PR, head SHA), `202` plus background job, diff size limit.
- Retry queue with backoff for offline, timed-out and errored reviews.
- Built frontend served by the backend; Dockerfile and Compose (port bound to loopback); deployment guide and `scripts/check_env.py`.
- CI (lint, tests on Python 3.11 and 3.12, frontend build, synthetic eval regression gate), pre-commit config, license, contributing guide.
- Frontend dependency vulnerabilities fixed (vite 6.0.7 to 6.4.5).

### 3. Evaluation harness (no results yet)

- Case schema and loader, git-based diff fetcher with cache, run harness (3 repeats, seeds), report generator, ablation matrix runner, candidate finder for labeling.
- Design in `docs/EVAL_DESIGN.md`. `docs/RESULTS.md` still says `TBD (not yet measured)`. No recall or false-positive figure exists, and the README says so.

## Test and CI status

- `python -m pytest -q`: 374 passed, 17 xfailed. The 17 xfails are the author-owned matcher spec tests (`tests/test_match.py`, `xfail(strict=False)`); they stay xfail until `is_catch` is implemented.
- `ruff check .`: passes.
- `npm run build` and `npm audit` in `frontend/`: build passes, 0 vulnerabilities.
- `gitleaks detect` over full history: no leaks found.
- CI, latest run on `v2-revamp` at the time of writing: https://github.com/mayankranjan-dev/coderev/actions/runs/41203536333 (commit `e1381de`). Check the run for the final commit before merging.
- The synthetic eval gate checks the harness with a stub provider. It says nothing about model quality.
- Docker: built, reached `(healthy)` and answered `/api/health` as expected, verified by the owner on 2026-10-31 (see `docs/DEPLOY.md`).

## Author-owned items still open

- [ ] Answer the AUTHOR DECISIONs in `docs/eval/LABELING_RULES.md` and `docs/eval/MATCHING_SPEC.md`.
- [ ] Label the cases in `eval/cases.yaml` and commit them before any model run.
- [ ] Implement the body of `is_catch` in `eval/match.py` (currently raises `NotImplementedError`).
- [ ] Run the pilot, baseline and ablations; publish with `python -m eval.report ... --write-results` ([docs/REFRESH_DOCS.md](REFRESH_DOCS.md)).
- [ ] Answer the prompts in the "Design decisions (author to complete)" section of `docs/DECISIONS.md`.
- [ ] Capture the screenshots listed in [docs/img/README.md](img/README.md) and add them to the README.
- [ ] Sync your local `.env` with `.env.example` (`scripts/check_env.py` lists the missing names).
- [ ] Decide on the untracked `PROJECT_CONTEXT.md` and `assets/` (see `docs/AUDIT.md` P3-2).
- [ ] Open and merge this pull request, then tag `v2.0`.

## Known limitations

See the Limitations section of the README. In short: a 1.5B local model, a small planned sample, subjective labels, contamination risk, Judge0 on Python 3.8, and SQLite that is ephemeral on free hosts.
