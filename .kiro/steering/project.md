# CodeRev Bot: Permanent Project Rules

These rules apply to every prompt in this workspace. If a request conflicts with a rule, stop and ask the owner before acting.

## 1. Secrets

- Never read, print, edit, copy, rename, or delete `.env`. That includes listing its keys. Use `.env.example` as the reference for variable names.
- Never print environment variable values, tokens, or webhook secrets in terminal output, logs, docs, or commit messages.
- Never stage or commit `.env`, `*.db`, `*.pem`, `*.key`, or any file containing a credential.
- Secrets in code come from environment variables only. Placeholders in examples must look obviously fake and must not match the secret-scan patterns below.

## 2. Git

- Commit after each logical unit of work. Keep commits small and use conventional commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `refactor:`.
- Use real timestamps only. Never set `GIT_AUTHOR_DATE` or `GIT_COMMITTER_DATE`, and never pass `--date`.
- Never use `--force`, `--amend` on a pushed commit, `rebase`, or `filter-branch`. Never rewrite history.
- Never `git push`. The owner pushes.
- Stage files by explicit path. Never use `git add -A` or `git add .`.
- Never modify git config. Never create, switch, or delete branches without being asked. Commit on the current branch.
- Do not commit untracked files you did not create in the current task (for example `PROJECT_CONTEXT.md`, `assets/`) unless asked.
- When finishing a task that included commits, show `git log --oneline` for the new commits.

## 3. Pre-commit gate

Run both checks before every commit. If either fails, do not commit; report the failure instead.

**Tests**

- Once `tests/` exists: `python -m pytest -q` from the repo root using `.venv`. It must exit 0.
- Until a test suite exists: run `python -m compileall -q main.py models.py database.py simulate_pr.py` and, if anything under `frontend/` changed, `npm run build` in `frontend/`. Both must exit 0. State in the report that no test suite exists yet.

**Secret scan**

- If `gitleaks` is installed, run it against staged changes. Any finding fails the gate.
- Otherwise run both commands below. The first must list no `.env`, `*.db`, `*.pem`, or `*.key` path. The second must print nothing.

```powershell
git diff --cached --name-only
git diff --cached -U0 | Select-String -Pattern '(ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|(?i)(secret|password|passwd|token|api[_-]?key)\s*[:=]\s*[''"][^''"\s]{12,}[''"]'
```

- The fallback scan is a pattern match, not a real scanner. Any hit fails the gate, even a suspected false positive. Ask the owner how to proceed.

## 4. Metrics and claims

- Never invent, estimate, or round up a metric. Every number in `README.md`, `docs/`, or a commit message must come from a command that was actually run in this repo. Cite the command or eval run ID next to it.
- If a number is not measured yet, write `TBD (not yet measured)`. Never put a plausible-looking placeholder number.
- Pure arithmetic (for example a confidence-interval width computed from a sample size) is allowed if labeled as a calculation, not a result.
- Do not add new capability claims ("detects X") unless an eval run supports them.

## 5. Regressions that must stay out

Check these before committing any change to the files involved.

1. **Zero-state Landing.** In `frontend/src/App.jsx`, `selectedId` starts as `null` and is never auto-filled from the fetched list. The `selected` memo has no fallback to `reviews[0]`. `Landing` renders when `mode === "browse"` and nothing is selected.
2. **Judge0, not Piston.** The sandbox stays Judge0 CE (`JUDGE0_*` config, `{"source_code", "language_id"}` payload). Do not reintroduce Piston config, URLs, or payload shapes.
3. **"Correction: ..." stays on the same line as the severity tag.** The prompt must require this, and the parser splits on newlines.
4. **No concrete few-shot examples in the review prompt.** Format examples use placeholders such as `<bug or security issue>` and `<specific fix>`, never real-looking code, identifiers, or vulnerability names. An eval-only prompt variant that tests this rule may exist under `eval/` only with the owner's explicit approval, and it must never be importable by the production path.
5. **Terminal stays dark.** The `Terminal` component keeps `bg-slate-900` (body) and `bg-slate-950` (header). Do not convert it to the light palette.

## 6. Dependencies

- Minimal dependencies. Prefer the standard library and packages already in `requirements.txt` / `frontend/package.json`.
- Every new dependency (Python package, npm package, CLI tool, or external service) needs an entry in `docs/DECISIONS.md` *before* it is added. The entry states what it is, why it is needed, the alternatives considered, and why the stdlib or an existing dependency is not enough.
- Pin exact versions. Runtime Python deps go in `requirements.txt`; dev/test-only deps go in `requirements-dev.txt`.

## 7. Author-owned components

These are written by the owner, not by Kiro:

- The labeling rules and the label data (`eval/data/*.jsonl` and `docs/eval/LABELING_RULES.md` decisions).
- The body of the "what counts as a catch" matching function specified in `docs/eval/MATCHING_SPEC.md`.

Kiro may edit the spec documents when asked, and may create a function signature that raises `NotImplementedError` when asked. Kiro must not write or change label entries, choose labels, or implement or "fix" the matching logic, even if a test fails because of it. Report the problem instead.

## 8. Scope

- Do not write feature code unless the prompt asks for it.
- Never run long-lived processes (dev servers, `ollama serve`, watchers) in the foreground. Ask the owner to start them.
- The eval design lives in `docs/EVAL_DESIGN.md`. Changes to methodology are made there first, then in code.
