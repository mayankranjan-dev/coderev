# Labeling Rules (AUTHOR-OWNED)

> **AUTHOR-OWNED.** The owner writes the final rules and every label. Kiro may restructure this document when asked, but must not decide open questions, choose items, or write or edit entries in `eval/data/*.jsonl` (see `.kiro/steering/project.md`, section 7).
>
> This file is a spec skeleton. Items marked **AUTHOR DECISION** need the owner's answer before labeling starts. The "suggested default" next to each is a starting point, not a decision.

## 1. Purpose

Produce about 50 bug items and about 30 control items whose labels are fixed before any model sees them, following a procedure another person could repeat.

## 2. Candidate repositories

Requirements:

- Public, Python-majority, actively maintained, with an OSI-approved license.
- Enough post-cutoff history to supply items (cutoff from `docs/EVAL_DESIGN.md` §6.1, default 2025-01-01).
- A mix of domains (web, data, CLI, libraries), so results are not about one codebase.

**AUTHOR DECISION 2.1:** The repository list, written down before sampling. *Suggested default: 8 to 12 repos.*

**AUTHOR DECISION 2.2:** The maximum items per repo. *Suggested default: 5 bugs and 3 controls.*

## 3. Sampling procedure (must be deterministic)

**AUTHOR DECISION 3.1:** How candidates are found. *Suggested default: for each repo, walk first-parent history forward from the cutoff date and consider commits whose message or linked issue/PR describes a bug fix (for example, a linked issue labeled `bug`).*

**AUTHOR DECISION 3.2:** The stopping rule. *Suggested default: take the first qualifying commits in date order until the per-repo cap is reached. Skipping a qualifying commit is not allowed.*

Every rejected candidate is logged with its SHA and the reason (a rejection log outside the dataset files, format up to the owner). This is the main defense against cherry-picking.

## 4. Bug item inclusion criteria

A fix commit qualifies only if all of these hold:

- Exactly one parent (not a merge). `parent_sha` is that parent.
- Committer date on or after the cutoff.
- Changes at least one non-test `*.py` file.
- The bug is visible in the code at `parent_sha` and can be localized to one file. **AUTHOR DECISION 4.1:** Are multi-location bugs allowed, and if so does the label hold several ranges? *Suggested default: single file and single contiguous range only. Reject others in v1.*
- The defect is a behavior bug, not taste.

Exclude:

- Dependency bumps, version bumps, typo and docs-only changes, formatting-only changes, test-only changes, reverts, and generated code.
- Fixes whose bug only shows up in non-Python files (config, C extensions, templates).
- **AUTHOR DECISION 4.2:** The maximum fix size. *Suggested default: at most 3 non-test Python files and at most 50 changed lines, so the reverse diff fits a small model's context.*

## 5. Choosing the line range

The range refers to `buggy_file` at `parent_sha`, 1-based and inclusive.

**AUTHOR DECISION 5.1:** Which lines make up "the bug". *Suggested default: the smallest contiguous range in the parent file that the fix deletes or modifies, plus the line where a missing check should have gone if the fix only adds lines.*

**AUTHOR DECISION 5.2:** The rule for pure-addition fixes, where nothing in the parent is deleted. *Suggested default: the range is the line immediately before and after the insertion point.*

**AUTHOR DECISION 5.3:** The maximum range length. *Suggested default: 10 lines. Longer ranges make catches too easy, so reject the item instead.*

Ranges are chosen from the fix commit, its message, and its linked issue only, never after looking at any model output.

## 6. Category taxonomy

The list is fixed before labeling starts, and each item gets exactly one category.

**AUTHOR DECISION 6.1:** The final taxonomy. *Suggested starting list:*

- `logic`: wrong condition, wrong operator, off-by-one
- `none-handling`: missing None/empty check
- `exception-handling`: wrong, missing, or too-broad exception handling
- `type-or-api-misuse`: wrong argument, wrong return type, misused library API
- `state-mutation`: shared mutable state, aliasing, mutable default argument
- `resource`: leaked file, connection, or lock
- `concurrency`: race, async misuse
- `security`: injection, path traversal, unsafe deserialization, auth check
- `edge-input`: unicode, empty input, boundary values
- `other`: used sparingly, with a note in the rejection or label log

## 7. Control item criteria

- Same repos and same date window as the bug items, so style and era match.
- Single-parent commit that changes non-test Python code.
- Not a bug fix by its message or linked issue (features, refactors, performance work).
- **AUTHOR DECISION 7.1:** Excluding controls that a later commit fixed. *Suggested default: reject a control if a later commit within 90 days references it or reverts it.*
- **AUTHOR DECISION 7.2:** Size matching. *Suggested default: changed-line count within the same range as bug items (§4.2).*
- **AUTHOR DECISION 7.3:** Schema field names for controls. *Suggested default: reuse `fix_sha` for the control commit and set `label` to `null`. Alternatively, rename the field to `commit_sha` in both files.*

## 8. Quality checks before freezing

- Every item passes the load-time validation in `docs/EVAL_DESIGN.md` §2.2.
- **AUTHOR DECISION 8.1:** Second labeler. *Suggested default: if available, a second person independently labels about 10 bug items, and agreement is reported. If not, the results state that labels are single-annotator.*
- Commit the dataset files before any run. After that, a label change means a new dataset version and a full re-run.
