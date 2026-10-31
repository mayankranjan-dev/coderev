# Screenshot capture list

Screenshots are taken by the owner. None exist yet. Save each file in this folder with the exact filename below, as PNG. The README references a screenshot only after its file exists here.

Before capturing: use a clean database (or only reviews you are happy to show), a 1280 px or wider browser window, and no secrets, tokens or personal data visible (check the address bar, the API key field and the browser's bookmarks bar). Crop to the app, not the whole desktop.

| # | Filename | What to capture | How to get there |
|---|---|---|---|
| 1 | `landing.png` | The zero-state landing page: centered welcome, nothing selected, no review auto-opened. | Open the app at `http://127.0.0.1:8000` (built frontend) with no review selected. |
| 2 | `manual-review.png` | A manual review with structured findings: severity tags, file and line location, and the correction text visible. | Manual review mode, submit a short snippet, wait for the result. Needs Ollama running. |
| 3 | `retry-queued.png` | A review in the retry-queued state (`llm_offline_pending` or `llm_timeout`), showing it is waiting for a retry and not lost. | Stop Ollama, submit a manual review, then capture before Ollama is restarted. |
| 4 | `run-code-terminal.png` | The Run Code terminal with output, in the dark terminal style. | Click Run Code on a short Python snippet that prints something. Uses the public Judge0 instance. |
| 5 | `eval-results.png` | The generated evaluation results page or table (`docs/RESULTS.md` rendered, or the report output). Only capture this after a real run has been written with `python -m eval.report --write-results`. Do not capture the TBD placeholders. | After the real eval runs described in `docs/eval/AUTHOR_TODO.md`. |
| 6 | `ci-green.png` | The GitHub Actions run page with every job green (Lint and test on Python 3.11 and 3.12, Frontend build, Synthetic eval regression gate). | https://github.com/mayankranjan-dev/coderev/actions, open the latest run on the branch you are publishing. |

## Embedding in the README

Once a file exists, add it to the README as a Markdown image whose path is `docs/img/<filename>`, and delete the matching "screenshot to add" line. Until then, the README keeps a marked "screenshot to add" line for that image.
