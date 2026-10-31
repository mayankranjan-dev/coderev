# Task Progress

Hardening, reviewer engine, and eval harness task. Resume rule: read this file and continue from the first item not marked done. A commit's own hash is filled in by the next commit, so the last finished item may show its hash only in the working tree.

| Item | Status | Commit |
|---|---|---|
| A0 pytest scaffolding | done | 94d2e2d |
| A1 API key on review/execute endpoints | done | 67a2c70 |
| A2 rate limiting and CORS | done | b3f1695 |
| A3 idempotent, non-blocking webhook | done | e54f41d |
| A4 retry queue | done | 76e3bae |
| A5 dependency and config drift | done | fd8014c |
| A6 serve built frontend | done | 08c6e0b |
| B1 review provider interface | done | 751fd04 |
| B2 structured findings and tolerant parser | done | d9a8bb3 |
| B3 diff line mapping and chunking | done | 327d71d |
| B4 typed failure handling with bounded retry | done | c732373 |
| B5 versioned prompts, placeholder-only | done | df0fa5a |
| B6 frontend adapted to structured findings | done | 69ef48e |
| C1 case schema and loader | done | 5c91167 |
| C2 diff fetcher with cache | done | 41e6d01 |
| C3 run harness | done | 2ccae80 |
| C4 author-owned matcher stub and xfail tests | done (stub only; body is author-owned) | bd22b3d |
| C5 report generator | done | a09528c |
| C6 ablation matrix runner | done | 1decdb1 |
| C7 harness tests with stub provider | done | ffcb629 |
| C8 labeling guide and author checklist | done | 8a458ab |
| F1 verification and audit update | done | f14f4f9 |
| F2 final report | done (report in chat) | bf92f72 |

Second task: CI, audit closure, candidate finder, packaging.

| Item | Status | Commit |
|---|---|---|
| D0 commit pending progress log | done | bf92f72 |
| D1 CI workflow, ruff config, lint fixes | done | 3218e0d |
| D2 synthetic eval regression gate | done | 15647a3 |
| D3a size limits for manual submissions and diffs | done | d6f4c89 |
| D3b pagination on GET /api/reviews | done | 9308088 |
| D4 candidate finder (eval/suggest.py) | done | 2e79bed |
| D5 Docker packaging | done (image not built: Docker daemon not running) | 7f61bd2 |
| D6 deployment guide and env check script | done | ba9e744 |
| D7 pre-commit, license, contributing | done | e1e6166 |
| D8 verification and audit update | done (hash shown is the previous commit, e1e6166; this row's own commit hash is not known until it exists) | e1e6166 |
