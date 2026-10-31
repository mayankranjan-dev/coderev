import asyncio

import pytest

from reviewer import diffutil as D
from reviewer.engine import EngineConfig, run_review
from reviewer.findings import Finding
from reviewer.providers import ProviderError, StubProvider

TWO_FILES = (
    "diff --git a/pkg/a.py b/pkg/a.py\n"
    "index 111..222 100644\n"
    "--- a/pkg/a.py\n"
    "+++ b/pkg/a.py\n"
    "@@ -10,4 +10,5 @@ def f():\n"
    "     keep1\n"
    "-    old\n"
    "+    new1\n"
    "+    new2\n"
    "     keep2\n"
    "     keep3\n"
    "@@ -40,2 +41,2 @@\n"
    "-    x = 1\n"
    "+    x = 2\n"
    "     y\n"
    "diff --git a/pkg/b.py b/pkg/b.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/pkg/b.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+line one\n"
    "+line two\n"
)


def test_parses_files_and_paths():
    files = D.parse_unified_diff(TWO_FILES)
    assert [f.path for f in files] == ["pkg/a.py", "pkg/b.py"]
    assert files[1].old_path is None
    assert len(files[0].hunks) == 2


def test_hunk_lines_map_to_file_lines():
    hunk = D.parse_unified_diff(TWO_FILES)[0].hunks[0]
    mapped = [(ln.kind, ln.old_no, ln.new_no) for ln in hunk.lines]
    assert mapped == [
        (" ", 10, 10),
        ("-", 11, None),
        ("+", None, 11),
        ("+", None, 12),
        (" ", 12, 13),
        (" ", 13, 14),
    ]
    assert hunk.file_line(4) == (12, 13)


def test_second_hunk_numbering_restarts_from_header():
    fd = D.parse_unified_diff(TWO_FILES)[0]
    assert fd.diff_line_to_new_line(1, 1) == 41
    assert fd.diff_line_to_new_line(1, 2) == 42


def test_new_and_added_line_sets():
    fd = D.parse_unified_diff(TWO_FILES)[0]
    assert fd.added_line_numbers() == {11, 12, 41}
    assert fd.new_line_numbers() == {10, 11, 12, 13, 14, 41, 42}


def test_deleted_file_uses_old_path():
    text = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    [fd] = D.parse_unified_diff(text)
    assert fd.path == "gone.py" and fd.new_path is None
    assert [ln.old_no for ln in fd.hunks[0].lines] == [1, 2]


def test_no_newline_marker_and_default_counts():
    text = "--- a/x.py\n+++ b/x.py\n@@ -3 +3 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    [fd] = D.parse_unified_diff(text)
    assert [(ln.kind, ln.old_no, ln.new_no) for ln in fd.hunks[0].lines] == [("-", 3, None), ("+", None, 3)]


def test_preamble_and_non_diff_input():
    assert D.parse_unified_diff("just some code\nx = 1\n") == []
    assert not D.is_unified_diff("x = 1")
    files = D.parse_unified_diff("commit message\n\n" + TWO_FILES)
    assert len(files) == 2


def test_strip_ab_prefix():
    assert D.strip_ab_prefix("a/pkg/x.py") == "pkg/x.py"
    assert D.strip_ab_prefix("b/x.py") == "x.py"
    assert D.strip_ab_prefix("/dev/null") is None
    assert D.strip_ab_prefix("lib/x.py") == "lib/x.py"


def test_finding_in_diff():
    files = D.parse_unified_diff(TWO_FILES)
    assert D.finding_in_diff(Finding("CRITICAL", "b/pkg/a.py", 12, 12, "m"), files)
    assert D.finding_in_diff(Finding("CRITICAL", "pkg\\a.py", 30, 41, "m"), files)
    assert not D.finding_in_diff(Finding("CRITICAL", "pkg/a.py", 20, 30, "m"), files)
    assert not D.finding_in_diff(Finding("CRITICAL", "pkg/c.py", 11, 11, "m"), files)
    assert not D.finding_in_diff(Finding("CRITICAL", None, None, None, "m"), files)


# --- chunking ---------------------------------------------------------------


def test_whole_fits_in_one_chunk():
    plan = D.plan_chunks(TWO_FILES, D.WHOLE, 10_000)
    assert len(plan.chunks) == 1 and plan.chunks[0].text == TWO_FILES
    assert not plan.truncated


def test_per_file_one_chunk_per_file():
    plan = D.plan_chunks(TWO_FILES, D.PER_FILE, 10_000)
    assert [c.path for c in plan.chunks] == ["pkg/a.py", "pkg/b.py"]
    assert "".join(c.text for c in plan.chunks) == TWO_FILES
    assert not plan.truncated


def test_whole_over_budget_cuts_at_hunk_boundary_and_records_it():
    first_file = D.parse_unified_diff(TWO_FILES)[0]
    budget = len(first_file.header) + len(first_file.hunks[0].raw) + 5
    plan = D.plan_chunks(TWO_FILES, D.WHOLE, budget)
    [chunk] = plan.chunks
    assert plan.truncated and chunk.truncated
    assert chunk.text == first_file.header + first_file.hunks[0].raw
    assert chunk.omitted_hunks == 2  # second hunk of a.py + the b.py hunk
    assert plan.omitted_files == ["pkg/b.py"]
    assert len(chunk.text) <= budget


def test_per_file_truncates_each_file_independently():
    first_file = D.parse_unified_diff(TWO_FILES)[0]
    budget = len(first_file.header) + len(first_file.hunks[0].raw)
    plan = D.plan_chunks(TWO_FILES, D.PER_FILE, budget)
    assert plan.chunks[0].truncated and plan.chunks[0].omitted_hunks == 1
    assert not plan.chunks[1].truncated
    assert plan.truncated


def test_oversized_single_hunk_is_cut_on_a_line_boundary():
    body = "".join(f"+line {i}\n" for i in range(100))
    text = f"--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,100 @@\n{body}"
    plan = D.plan_chunks(text, D.WHOLE, 200)
    chunk = plan.chunks[0]
    assert chunk.truncated and chunk.text.endswith("\n") and len(chunk.text) <= 200
    assert chunk.text.splitlines()[2].startswith("@@")


def test_snippet_is_cut_on_a_line_boundary():
    text = "a = 1\n" * 100
    plan = D.plan_chunks(text, D.PER_FILE, 50)
    assert plan.chunks[0].path is None and plan.chunks[0].truncated
    assert plan.chunks[0].text == "a = 1\n" * 8


def test_plan_to_dict_has_no_code():
    data = D.plan_chunks(TWO_FILES, D.PER_FILE, 10_000).to_dict()
    assert "text" not in data["chunks"][0] and data["chunks"][0]["chars"] > 0


def test_bad_strategy_and_budget():
    with pytest.raises(ValueError):
        D.plan_chunks("x", "sideways", 10)
    with pytest.raises(ValueError):
        D.plan_chunks("x", D.WHOLE, 0)


def test_char_budget_is_arithmetic_on_num_ctx():
    # (8192 - 1024 - ceil(300/3)) tokens * 3 chars
    assert D.char_budget(8192, 300, 1024) == (8192 - 1024 - 100) * 3
    assert D.char_budget(100, 10_000) == 0


# --- engine uses the plan ---------------------------------------------------


def test_engine_per_file_sends_one_request_per_file():
    stub = StubProvider(replies=["NITPICK: pkg/a.py:11 x", "NO_ISSUES_FOUND"])
    result = asyncio.run(run_review(TWO_FILES, stub, lambda code: f"P\n{code}", EngineConfig(chunking=D.PER_FILE)))
    assert len(stub.calls) == 2
    assert [f.file for f in result.findings] == ["pkg/a.py"]
    assert not result.truncated


def test_engine_records_truncation_from_budget_and_context():
    stub = StubProvider(replies=["NO_ISSUES_FOUND"], num_ctx=100, prompt_eval_count=100)
    result = asyncio.run(run_review(TWO_FILES, stub, lambda c: c, EngineConfig(budget_chars=10_000)))
    assert result.truncated  # context window filled
    stub2 = StubProvider(replies=["NO_ISSUES_FOUND"])
    result2 = asyncio.run(run_review(TWO_FILES, stub2, lambda c: c, EngineConfig(budget_chars=120)))
    assert result2.plan.truncated and result2.truncated


def test_engine_stops_on_provider_error():
    stub = StubProvider(replies=[ProviderError("unreachable", "down")])
    result = asyncio.run(run_review(TWO_FILES, stub, lambda c: c, EngineConfig(chunking=D.PER_FILE)))
    assert len(stub.calls) == 1
    assert result.provider_error.error_kind == "unreachable"
