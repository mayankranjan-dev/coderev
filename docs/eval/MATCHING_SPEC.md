# "What Counts as a Catch": Matching Function Spec (AUTHOR-OWNED)

> **AUTHOR-OWNED.** This document is the spec. The owner writes the implementation. Kiro may create the function signature with a `NotImplementedError` body when asked, but must not implement, modify, or "fix" the matching logic, even when a test fails (see `.kiro/steering/project.md`, section 7).
>
> Items marked **AUTHOR DECISION** need the owner's answer before implementation. The "suggested default" is a starting point, not a decision.

## 1. Purpose

Decide whether one parsed reviewer finding counts as catching one labeled bug. Recall in `docs/EVAL_DESIGN.md` §4 is defined entirely by this function, so it must be simple, deterministic, and documented.

## 2. Interface

```
is_catch(finding, label, tolerance) -> bool
```

Inputs:

- `finding`: one structured finding from the reviewer's parser, with fields `severity` (`CRITICAL` | `WARNING` | `NITPICK`), `path` (str or None), `line_start` (int or None), `line_end` (int or None), and `text` (str).
- `label`: the item's label, with fields `buggy_file` (str), `line_start` (int), `line_end` (int), and `category` (str).
- `tolerance`: a non-negative int, the number of lines. It comes from the eval config and is never hardcoded.

Output: `True` only if the finding catches the labeled bug.

An item is "caught" if `is_catch` is `True` for at least one finding in that item's review. Item-level aggregation lives in the harness, not here.

## 3. Required properties

1. **Pure and deterministic.** No I/O, no randomness, no model calls. Same inputs give the same output.
2. **File must match.** A finding with a path that doesn't refer to `buggy_file` is never a catch.
3. **Location must be present.** A finding with no `path`, or no parseable line, is never a catch. Unlocated findings are counted separately by the harness.
4. **Line overlap with tolerance.** The finding's range `[line_start, line_end]` must overlap the label's range widened by `tolerance` on both sides: `[label.line_start - tolerance, label.line_end + tolerance]`. Ranges are inclusive.
5. **Monotone in tolerance.** If `is_catch` is `True` at tolerance `t`, it is `True` at every tolerance above `t`.
6. **Severity is ignored.** Severity filtering (for "CRITICAL-only recall") is done by the caller, not inside this function.
7. **Text is ignored.** The finding's description is not inspected. Description quality is out of scope for v1. This keeps the function objective.

## 4. Open decisions

**AUTHOR DECISION 4.1: Path normalization.** How to compare `finding.path` with `buggy_file`. Diff headers add `a/` and `b/` prefixes, and models may print a bare filename or use backslashes. *Suggested default: strip a leading `a/` or `b/`, convert `\` to `/`, then require an exact match, OR a match on the final path components when the bare filename is unique within the item's diff. Anything else is not a match.*

**AUTHOR DECISION 4.2: Single-line findings.** *Suggested default: a finding with one line `n` is treated as the range `[n, n]`.*

**AUTHOR DECISION 4.3: Reversed or invalid ranges.** For example `line_start > line_end`, zero, or negative lines. *Suggested default: swap a reversed range; treat zero or negative lines as unlocated (never a catch).*

**AUTHOR DECISION 4.4: Overly broad findings.** A finding claiming lines 1-500 would overlap almost anything. *Suggested default: a finding wider than a configurable `max_finding_span` (from the eval config) is never a catch, and the harness counts it.*

**AUTHOR DECISION 4.5: Line coordinate system.** Labels use parent-file line numbers. The reverse diff's `+` side also uses parent-file numbers, so a model quoting `+` side numbers matches directly. *Suggested default: interpret finding lines as `+` side (parent-file) numbers and record that in the prompt. Do not attempt to remap hunk-relative numbers.*

**AUTHOR DECISION 4.6: Per-file chunking.** In the per-file ablation each request sees one file, so the model may omit the path. *Suggested default: the harness fills `path` from the chunk's file when the finding has none, and records that it did. The matching function itself stays unchanged.*

## 5. Example cases for the owner's tests

The cases below describe expected behavior for tests the owner will write. They use the suggested defaults. Change them if a decision changes.

Label for all cases: `buggy_file = "pkg/mod.py"`, range 40-42.

| # | Finding path | Finding lines | Tolerance | Expected | Why |
|---|---|---|---|---|---|
| 1 | `pkg/mod.py` | 41 | 0 | catch | inside range |
| 2 | `b/pkg/mod.py` | 40-42 | 0 | catch | prefix stripped (4.1) |
| 3 | `pkg/mod.py` | 45 | 0 | miss | outside range |
| 4 | `pkg/mod.py` | 45 | 3 | catch | within tolerance |
| 5 | `pkg/mod.py` | 46 | 3 | miss | one line past tolerance |
| 6 | `pkg/other.py` | 41 | 10 | miss | wrong file |
| 7 | none | 41 | 10 | miss | unlocated |
| 8 | `pkg/mod.py` | none | 10 | miss | unlocated |
| 9 | `pkg/mod.py` | 42-40 | 0 | catch | reversed range swapped (4.3) |
| 10 | `pkg/mod.py` | 0 | 100 | miss | invalid line (4.3) |
| 11 | `pkg\mod.py` | 41 | 0 | catch | backslash normalized (4.1) |
| 12 | `pkg/mod.py` | 30-38 | 1 | miss | ends one line before the widened range (39) |
| 13 | `pkg/mod.py` | 30-39 | 1 | catch | touches widened range |

The owner should also add a property test for monotonicity (§3.5) across tolerances 0-20.
