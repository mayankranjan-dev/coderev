# Evaluation Design: CodeRev Bot on Real Historical Bugs

Status: design only. No harness exists yet, and no results exist yet. Every metric below is `TBD (not yet measured)` until an eval run produces it.

## 1. Questions this eval answers

1. **Recall.** When the reviewer is shown a change that introduces a real, historically fixed bug, how often does it flag the right file and lines?
2. **False positives.** When it is shown an ordinary change that is not a bug, how often does it raise an alarm anyway?
3. **Cost and reliability.** How many findings does it produce, how long does a review take, how often does the output fail to parse, and how often is a retry needed?
4. **Ablations.** How do model size, prompt wording, chunking, and retries change the numbers above?

Precision on bug items is **not** a headline metric. A bug item is labeled with one known bug, and an off-target finding may still be a real issue. Off-target findings are reported as a count, not as false positives.

## 2. Dataset

### 2.1 What is stored

Only metadata and the owner's labels are committed. Diffs and source files are fetched at runtime and never copied into the repo.

Two JSON Lines files (paths are proposals until the harness exists):

- `eval/data/bugs.jsonl`: about 50 bug-fix commits.
- `eval/data/controls.jsonl`: about 30 non-bug commits.

One object per line:

```json
{"id": "<short stable id>", "repo_url": "https://github.com/<owner>/<repo>", "fix_sha": "<40-char sha>", "parent_sha": "<40-char sha>", "label": {"buggy_file": "<path at parent_sha>", "line_start": 0, "line_end": 0, "category": "<category>"}}
```

- For controls, `fix_sha` holds the control commit and `label` is `null`. (Field naming for controls is an owner decision; see `docs/eval/LABELING_RULES.md`.)
- `line_start` and `line_end` are 1-based, inclusive, and refer to `buggy_file` **as it exists at `parent_sha`**, the buggy version.
- The harness may derive other values at runtime (commit date, diff size, file list). Derived values go in run artifacts, not in the dataset files.
- A bug can span more than one place. Whether a label may have multiple ranges is an owner decision in the labeling rules. The schema above shows the single-range form.

Labeling rules (selection criteria, how to pick the line range, the category taxonomy) are **AUTHOR-OWNED**: `docs/eval/LABELING_RULES.md`.

### 2.2 Validation at load time

The harness rejects an item, and the run fails loudly rather than skipping it silently, if:

- `parent_sha` is not the first parent of `fix_sha`, or `fix_sha` is a merge commit.
- `buggy_file` does not exist at `parent_sha`, or the line range is outside the file.
- The item's committer date is earlier than the contamination cutoff for any model in the config (section 6.1). Items may be kept for a separate "pre-cutoff" report, but are never mixed into the headline numbers.

### 2.3 Fetching

The harness uses the `git` CLI: `git fetch --depth=2 <repo_url> <fix_sha>` into a gitignored cache (`eval/.cache/`), then `git diff` and `git show`. This adds no Python dependency (see `docs/DECISIONS.md`). The cache directory must be added to `.gitignore` in the same commit that introduces the harness.

### 2.4 What the reviewer sees

| Item type | Input to reviewer | Why |
|---|---|---|
| Bug | Reverse of the fix: `git diff <fix_sha> <parent_sha>`. This is a change that *introduces* the bug, which is the situation a PR reviewer is in. | The bug's location in the parent file maps directly onto the `+` side of this diff. |
| Control | Forward diff: `git diff <parent_sha> <fix_sha>`. | An ordinary change, reviewed the way a real PR would be. |

Both use the same filters:

- Only `*.py` files.
- Test files are excluded by default (paths matching `tests/`, `test_*.py`, `*_test.py`). Otherwise reversing a fix shows the reviewer a change that deletes the regression test, which is an unnatural hint. Configurable.
- Same context-line setting (`-U`, default 3, configurable).

The reverse-diff approach has known validity costs (section 6.4).

## 3. Reviewer output contract required by the eval

The current production output cannot be scored by location (`docs/AUDIT.md`, P0-2). Before any run, the prompt and parser need these changes, which are feature work to be done in a separate prompt:

- Each finding line: `<SEVERITY>: <path>:<line or start-end> <description> Correction: <fix>`. Exact syntax is decided at implementation time. "Correction:" stays on the same line as the severity tag.
- The format example uses placeholders only, never concrete code (project rule).
- An explicit sentinel line for "no issues found", so a clean review can be told apart from a format failure.
- The parser returns structured findings `(severity, path, line_start, line_end, text)` and flags unparseable lines. It does not only count them.

The eval calls the same prompt builder and parser as production, so it measures the shipped system. Prompt variants are injected as alternate builders.

## 4. Metrics

All metrics are computed per run, then aggregated across the 3 runs of a config (section 5.3).

| Metric | Definition |
|---|---|
| **Recall** (primary) | Bug items with at least one *catch* divided by bug items reviewed. A catch is decided by the matching function, which is **AUTHOR-OWNED**: `docs/eval/MATCHING_SPEC.md`. It requires the right file and a line range within the configured tolerance. Any severity counts. |
| Recall, CRITICAL only | Same, counting only CRITICAL findings. |
| Recall by category | Recall within each bug category. Reported with per-category n, since cells will be small. |
| Recall vs tolerance | Recall at tolerance 0, 3, 5, and 10 lines, so readers can see how sensitive the headline number is to the matching rule. |
| **False-positive rate** (primary) | Control items with at least one CRITICAL finding divided by control items reviewed. A secondary version counts findings of any severity. |
| Findings per review | Mean number of parsed findings per review, by severity, reported separately for bug and control items. |
| Off-target findings | On bug items: findings that are not catches. A count, not called false positives. |
| Unlocated finding rate | Findings with a valid severity tag but no parseable location, divided by all findings. These can never be catches. |
| **Parse failure rate** | Reviews whose final output contains neither one valid finding line nor the no-issues sentinel, divided by reviews attempted. Transport failures are excluded and counted separately. |
| Retries needed | With retry on: the number of extra attempts per review before a parseable output (0, 1, 2, ...). Reported as a distribution and a mean. |
| Transport failure rate | Reviews that failed on connection, HTTP error, or timeout, divided by reviews attempted. |
| **Latency** | Wall-clock seconds from request sent to response received, measured by the harness. Reported as median, p90, mean, and sample std. Per-file chunking reports the sum across chunks for an item. |
| Over production timeout | Fraction of reviews slower than the production `OLLAMA_TIMEOUT_SECONDS` default, i.e. reviews production would have marked `llm_offline_pending`. |
| Truncation suspected | Reviews where Ollama's `prompt_eval_count` reached the configured `num_ctx`, meaning part of the input was likely dropped. |

The eval sets a long request timeout so slow reviews still complete and their latency is recorded. That timeout is a config value and is recorded with the run.

## 5. Experimental design

### 5.1 Baseline config

- Model: `qwen2.5-coder:1.5b` (production default).
- Prompt: the production prompt after the section 3 changes.
- Whole diff in one request.
- Retry on parse failure: on, max 2 extra attempts.
- `temperature` 0.2 (production value), explicit `num_ctx`, explicit `seed` per run.

### 5.2 Ablations

One factor at a time from the baseline. A full factorial is not run; the call count is too high for local hardware. The OFAT design means interactions between factors are not measured.

| Factor | Levels | Notes |
|---|---|---|
| Model size | `qwen2.5-coder` 0.5b, 1.5b, 3b, 7b | Same family, so size is the only change. Each level is run only if it fits on the hardware; skipped levels are reported as skipped. Ollama model digest recorded. |
| Prompt variant | (A) baseline; (B) no issue checklist; (C) no "Correction:" requirement; (D) eval-only concrete few-shot examples | B tests the claim that without a checklist the model stops after one finding. C tests whether requiring corrections costs recall. D tests the project rule against concrete examples; it is eval-only, needs owner approval, and must never be importable by production code. |
| Chunking | whole diff vs per file | Per file means one request per changed file, with findings unioned. Same `num_ctx` for both. |
| Retry | on (max 2) vs off | Retry re-sends the same prompt with the next seed. Only parse failures are retried; transport failures are not. |

### 5.3 Repeats and variance

- Each config runs **3 times** over the full dataset, with seeds 1, 2, and 3. Seeds are recorded.
- For each metric: report the mean of the 3 run-level values, the sample standard deviation, and the min and max.
- With only 3 runs, the std describes run-to-run noise only roughly. It does not describe uncertainty from the choice of items.
- Item-sampling uncertainty: report a 95% bootstrap interval for recall and FP rate, resampling items with replacement (10,000 resamples, stdlib `random`, fixed seed).
- Comparing two configs: paired by item, with a paired bootstrap of the difference. Differences whose interval includes 0 are reported as "no measurable difference", not as a win.

### 5.4 Run procedure

1. Freeze the dataset: commit `bugs.jsonl` and `controls.jsonl` **before** any model is run on them. Record the dataset's git blob hash in each run.
2. Pilot on at most 5 items to check the pipeline. Pilot results are discarded and never reported, and pilot items stay in the dataset.
3. Run all configs. No label is changed after any model output has been seen. If a label turns out to be wrong, the fix is a new dataset version, and all configs are re-run on it.
4. Generate the report from the stored artifacts only.

### 5.5 Run artifacts

Each run writes `eval/results/<run_id>/`:

- `config.json`: all settings, seeds, timeouts, `num_ctx`, and the exact command line.
- `env.json`: CodeRev Bot git SHA, dataset blob hash, Ollama version, model digest, OS, CPU/GPU, RAM, start and end timestamps.
- `outputs.jsonl`: per item and attempt: raw model output, parsed findings, latency, `prompt_eval_count`, `eval_count`, and error type. No diffs or source files.
- `summary.json`: computed metrics.

Numbers in `README.md` or `docs/` must cite a `run_id` (project rule). Committing `outputs.jsonl` is recommended so results are auditable. Raw outputs may quote short snippets of public code, which the owner should keep in mind.

### 5.6 Proposed entry points

These are a sketch, not implemented: `python -m eval.run --config <file> --runs 3` and `python -m eval.report <run_id>...`. The harness uses the stdlib plus `httpx` only.

## 6. Threats to validity

### 6.1 Training-data contamination

The models may have seen the fix commits, or discussion of them, during training. A model that has memorized a fix can "catch" the bug without understanding it.

- **Cutoffs.** Qwen2.5-Coder's technical report ([arXiv 2409.12186](https://arxiv.org/abs/2409.12186), September 2024) does not state a data cutoff that I found. A Qwen GitHub discussion lists June 2024 for Qwen2.5-Coder, but that is not an official statement ([QwenLM/Qwen3 #1093](https://github.com/QwenLM/Qwen3/discussions/1093)). Because the cutoff is uncertain, the rule uses the public release date as a conservative bound.
- **Rule.** Headline numbers use only fix commits whose committer date is after the latest release date of any model in the run. For the Qwen2.5-Coder family, the default cutoff is **2025-01-01**, chosen as a margin after the 2024 releases of all sizes. If a newer model is added to an ablation, the cutoff moves forward and every config is re-scored on the reduced set.
- **Residual risk.** Committer dates can be rewritten. The buggy code itself (as opposed to the fix) may predate the cutoff and appear in training data. Similar bugs in other repos may have been seen. None of these can be ruled out. Any pre-cutoff items are reported separately, as a contamination probe: if recall on pre-cutoff items is much higher, that suggests memorization.

### 6.2 Label subjectivity

One person writes all labels, and the line range and category involve judgment.

- Labels are written from the fix commit and its linked issue only, before any model sees the item (section 5.4).
- The tolerance sweep (section 4) shows how much recall depends on the exact range.
- If a second labeler is available, they label a subset independently. Report agreement on file, range overlap, and category. Without a second labeler, say so explicitly in the results.
- Category assignments come from a fixed taxonomy defined before labeling.

### 6.3 Small sample size

About 50 bug items and 30 controls give wide intervals.

- *Calculation, not a result:* at n = 50 and a true recall of 0.5, the normal-approximation 95% interval half-width is 1.96 × √(0.25 / 50) ≈ 0.14, about ±14 percentage points. At n = 30 controls the same formula gives ≈ 0.18.
- Per-category recall will have single-digit n per cell. It is reported with n and without claims.
- Bootstrap intervals are always shown next to point estimates. Small config differences are not claimed as improvements.

### 6.4 Construct validity of the reverse-diff setup

- A reverse fix diff is small and centered on the bug, which hints at the location. Recall measured this way is likely higher than on real PRs, where the bug sits inside unrelated changes. The context-line setting partly controls this. A "wide context" variant (larger `-U`) is optional.
- Reversed diffs can look unnatural, for example code that removes a guard clause. Controls are forward diffs, so the two sets differ in style as well as in bugginess. The report includes diff-size distributions for both sets so readers can see the size mismatch.

### 6.5 Control set is not proven bug-free

A control commit may contain a real bug that nobody has fixed yet. The FP rate is therefore an upper bound. The owner may adjudicate flagged control findings, but the unadjudicated rate is always reported too.

### 6.6 Selection bias

If items are hand-picked, the owner may unconsciously favor bugs that look catchable. Mitigation: a written, deterministic sampling procedure in the labeling rules (for example, walk each repo's history forward from the cutoff and take the first qualifying commits), plus a per-repo cap so no single repo dominates. Rejected candidates are logged with a reason.

### 6.7 Format compliance confounds detection

A model that spots a bug but formats the location wrongly scores as a miss. The unlocated-finding rate and parse failure rate are reported next to recall so readers can tell "did not see it" apart from "could not say where".

### 6.8 Environment

Latency depends on hardware, background load, and whether the model is already loaded. Hardware is recorded, and the first request of each run is a discarded warm-up. Results from different machines are not compared directly.
