# Contributing

Setup: `python -m venv .venv`, then `.venv\Scripts\python -m pip install -r requirements-dev.txt` and `npm ci` in `frontend/`.

Before committing, run these from the repo root. All must pass:

- `python -m pytest -q`
- `ruff check .`
- `gitleaks git --staged --no-banner`
- `npm run build` in `frontend/` if you changed anything there

`pre-commit run --all-files` runs ruff and gitleaks (`.pre-commit-config.yaml`). Do not run `pre-commit install`: this repo uses `core.hooksPath` for its pre-push guard, and pre-commit refuses to install over it.

Never commit `.env`, `*.db`, keys, or tokens. Use `.env.example` for variable names, and `python scripts/check_env.py` to compare names.

Commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `refactor:`, `chore:`. Do not rewrite pushed history. Changes to `main` go through a pull request.
