# Author Checklist (owner-only work)

Everything below must be done by the owner. The harness, loader, fetcher, runner, report, and ablation runner exist and are tested with stubs; no real eval result exists yet (`docs/RESULTS.md` says `TBD (not yet measured)`).

Commands assume PowerShell at the repo root with `.venv` set up: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`.

## 1. Decisions

- [ ] Answer every **AUTHOR DECISION** in `docs/eval/LABELING_RULES.md` (§2.1, §2.2, §3.1, §3.2, §4.1, §4.2, §5.1–5.3, §6.1, §7.1–7.3, §8.1).
- [ ] Confirm §7.3 (control field names). The case schema built for this task follows the task request: one file, a `split: bug | control` field, and `fix_sha` holding the control commit. If you want different names, say so and the loader changes.
- [ ] Answer every **AUTHOR DECISION** in `docs/eval/MATCHING_SPEC.md` (§4.1–4.6). Note for §4.4: the current signature has three arguments; a `max_finding_span` limit needs an extra keyword argument. Note for §4.6: the harness does not fill a missing path from the chunk file yet; ask if you want it.
- [ ] Confirm the headline `tolerance` in `eval/ablation.yaml` (currently 3, a placeholder for the run command, not a decision). The report also shows recall at 0, 3, 5, and 10.
- [ ] Decide whether eval-only prompt variants B–D from `EVAL_DESIGN.md` §5.2 are wanted. Only v1 and v2 exist. Variant D (concrete few-shot examples) needs your explicit approval and must live outside `prompts/`.

## 2. Label the dataset

- [ ] Label about 50 bug cases and about 30 control cases in `eval/cases.yaml`, following `eval/LABELING.md`.
- [ ] Keep the rejection log.
- [ ] Validate: `.\.venv\Scripts\python.exe -m eval.fetch` must report 0 errors.
- [ ] Commit `eval/cases.yaml` before any model run (dataset freeze).

## 3. Implement the matcher

- [ ] Write the body of `is_catch` in `eval/match.py` per `docs/eval/MATCHING_SPEC.md`. It currently raises `NotImplementedError`, and every recall figure in the report reads `TBD (matcher not implemented)` until it is done.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_match.py -rxX`. The 17 spec tests are `xfail(strict=False)`; once they pass they show as XPASS. Then remove the `AUTHOR_OWNED` marker and adjust any case whose decision differs from the suggested default.

## 4. Run the baseline

Start Ollama yourself in a separate terminal (`ollama serve`) and pull the model (`ollama pull qwen2.5-coder:1.5b`).

```powershell
# Pilot on 5 cases. Results go to a pilot- run id and are never reported.
.\.venv\Scripts\python.exe -m eval.run --config eval/configs/baseline.yaml --pilot 5

# Baseline: every case, seeds 1, 2, 3.
.\.venv\Scripts\python.exe -m eval.run --config eval/configs/baseline.yaml --repeats 3

# Report (use the run id printed by the previous command).
.\.venv\Scripts\python.exe -m eval.report <run_id> --tolerance 3

# Publish to docs/RESULTS.md (refuses stub and pilot runs).
.\.venv\Scripts\python.exe -m eval.report <run_id> --tolerance 3 --write-results
```

An interrupted run continues with `--resume <run_id>`. After the first fetch, `--offline` reruns use only the cache.

## 5. Run the ablation matrix

```powershell
# Optional: pull the other model sizes. Levels that are not pulled are reported as skipped.
ollama pull qwen2.5-coder:0.5b
ollama pull qwen2.5-coder:3b
ollama pull qwen2.5-coder:7b

# Check the pipeline without Ollama (stub provider; produces no results).
.\.venv\Scripts\python.exe -m eval.ablation --dry-run

# Real run: baseline plus one-factor variants, combined table in eval/results/ablation-<time>.md
.\.venv\Scripts\python.exe -m eval.ablation

# Publish the configs you want in docs/RESULTS.md, baseline first:
.\.venv\Scripts\python.exe -m eval.report <baseline_run_id> <variant_run_id> ... --tolerance 3 --write-results
```

### Runtime on CPU

Not measured yet: `TBD (not yet measured)`. No review has been run on this machine during this task, so there is no per-review latency to multiply.

The request count is arithmetic (a calculation, not a result):

- One config: 80 cases × 3 repeats = 240 reviews, each one request with whole-diff chunking.
- Retries add up to 2 requests per review that returns unparseable output (`max_retries: 2`).
- Per-file chunking sends one request per changed `.py` file in the case.
- Full matrix: baseline plus 6 variants = 7 configs × 240 = 1,680 reviews before retries and chunking, if every model size is pulled.

To get the runtime, run the pilot and read `Latency p50` from its report. Estimated runtime for one config is then about 240 × p50 seconds, and for the matrix about 1,680 × p50 seconds, with larger models slower than the 1.5b baseline.

## 6. After results exist

- [ ] Replace capability claims in `README.md` with measured numbers that cite a run id, or soften them (`docs/AUDIT.md` P3-1).
- [ ] Decide whether to commit any `eval/results/<run_id>/` (gitignored by default because raw model output can quote third-party code; add with `git add -f`).
