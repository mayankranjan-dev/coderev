# Refreshing the docs after the real evaluation run

The README currently says results are not yet measured (`docs/RESULTS.md` holds only `TBD (not yet measured)`). After the owner has run the real evaluation, follow these steps. Do them in order and do not hand-type any number.

## Prerequisites (owner work)

Complete sections 1 to 5 of [docs/eval/AUTHOR_TODO.md](eval/AUTHOR_TODO.md): decisions answered, `eval/cases.yaml` labeled and committed before any run, `is_catch` implemented, pilot checked, baseline run with 3 repeats, and the ablation matrix run.

## Steps

1. Note the run IDs printed by the baseline run and by each ablation variant you want published (baseline first).
2. Write the results file. The command refuses stub and pilot runs and records the exact command and date:

   ```powershell
   .\.venv\Scripts\python.exe -m eval.report <baseline_run_id> <variant_run_id> ... --tolerance 3 --write-results
   ```

   Use the tolerance you decided on in `eval/ablation.yaml`. The report also shows recall at tolerances 0, 3, 5 and 10.
3. Open `docs/RESULTS.md` and check that it now has real numbers, the command, the date and the run IDs. If any cell still says `TBD`, keep it that way in the README too.
4. Commit `docs/RESULTS.md` (and `eval/results/<run_id>/` only if you decided to commit run artifacts; see EVAL_DESIGN section 5.5 about quoted third-party code).
5. Re-run the docs refresh prompt (paste the text below into the assistant).
6. Capture `docs/img/eval-results.png` (see [docs/img/README.md](img/README.md)) and add it to the README once the file exists.
7. Fill in "What did the eval show that surprised me?" in [docs/DECISIONS.md](DECISIONS.md) yourself.

## Docs refresh prompt

> `docs/RESULTS.md` now contains a real run. Set RESULTS_STATE to REAL. In `README.md`: replace the "Hero claim" with the recall and false-positive numbers from `docs/RESULTS.md` only, and replace the "Evaluation status" section with the table from `docs/RESULTS.md`, the exact command and date, and the ablations. Cite the run IDs next to every number. Do not add any number that is not in `docs/RESULTS.md`. Update `docs/AUDIT.md` P3-1 and `docs/PR_BODY.md` if they mention the results as pending. Keep every limitation in the README. Do not touch `eval/match.py`, `eval/cases.yaml` or any label data.

## Check before committing

- Every number in the README appears in `docs/RESULTS.md`.
- The README still lists the limitations (small model, small sample, label subjectivity, contamination risk).
- Claims of the form "detects X" are backed by a run ID, or removed.
