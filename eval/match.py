"""
"What counts as a catch": the matching function (AUTHOR-OWNED).

The spec is docs/eval/MATCHING_SPEC.md. The owner writes the body of
is_catch(). Kiro created only the signature, this docstring, and the xfail
tests in tests/test_match.py, and must not implement or change the logic
(.kiro/steering/project.md §7).

Recall in docs/EVAL_DESIGN.md §4 is defined entirely by this function. Until
it is implemented, eval/report.py reports recall as "TBD (matcher not
implemented)" instead of a number.
"""


def is_catch(finding, label, tolerance: int) -> bool:
    """
    Decide whether one parsed reviewer finding catches one labeled bug.

    Inputs (MATCHING_SPEC §2):

    - finding: one structured finding from the reviewer's parser
      (reviewer.findings.Finding). Fields used by the spec: ``severity``
      (CRITICAL | WARNING | NITPICK), ``path`` (str or None; alias of
      ``Finding.file``), ``line_start`` (int or None), ``line_end``
      (int or None), and ``text`` (str; alias of ``Finding.message``).
    - label: the item's label (eval.cases.Label) with ``buggy_file`` (str),
      ``line_start`` (int), ``line_end`` (int), and ``category`` (str).
    - tolerance: a non-negative int, the number of lines. It comes from the
      eval config and is never hardcoded.

    Returns True only if the finding catches the labeled bug. An item is
    caught if this is True for at least one finding in its review; that
    aggregation lives in the harness (eval/report.py), not here.

    Required properties (MATCHING_SPEC §3):

    1. Pure and deterministic: no I/O, no randomness, no model calls.
    2. File must match: a path that does not refer to ``buggy_file`` is never
       a catch.
    3. Location must be present: no path, or no parseable line, is never a
       catch. (Unlocated findings are counted separately by the harness.)
    4. Line overlap with tolerance: ``[line_start, line_end]`` must overlap
       ``[label.line_start - tolerance, label.line_end + tolerance]``.
       Ranges are inclusive.
    5. Monotone in tolerance: True at tolerance t implies True at every
       tolerance above t.
    6. Severity is ignored (CRITICAL-only recall filters in the caller).
    7. Text is ignored.

    Open AUTHOR DECISIONS (MATCHING_SPEC §4), with the spec's suggested
    defaults, which are not decisions:

    - 4.1 Path normalization. Suggested: strip a leading ``a/`` or ``b/``,
      convert ``\\`` to ``/``, then require an exact match, or a match on the
      final path components when the bare filename is unique within the
      item's diff.
    - 4.2 Single-line findings. Suggested: line ``n`` is the range ``[n, n]``.
      (The parser already sets ``line_end = line_start`` for one line.)
    - 4.3 Reversed or invalid ranges. Suggested: swap a reversed range; zero
      or negative lines are unlocated (never a catch). The parser passes the
      model's numbers through unchanged so this function decides.
    - 4.4 Overly broad findings. Suggested: wider than a configurable
      ``max_finding_span`` is never a catch. This may need an extra keyword
      argument; the signature above is the spec's three-argument form.
    - 4.5 Line coordinates. Suggested: finding lines are + side
      (parent-file) numbers. The prompts ask for new-file (+ side) numbers.
    - 4.6 Per-file chunking. Suggested: the harness fills a missing path
      from the chunk's file. The harness does not do this yet.

    Expected behavior for the example cases is in MATCHING_SPEC §5 and in
    tests/test_match.py.
    """
    # TODO(author): implement per docs/eval/MATCHING_SPEC.md. Author-owned:
    # Kiro must not write this body.
    raise NotImplementedError("is_catch is author-owned; see docs/eval/MATCHING_SPEC.md")
