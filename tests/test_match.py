"""
Example cases for the author-owned matching function, transcribed from
docs/eval/MATCHING_SPEC.md §5 (which uses the suggested defaults). They are
xfail until the owner implements eval.match.is_catch. strict=False so they
start passing (XPASS) without edits once it is implemented; the owner may
then remove the marker and adjust any case whose decision changed.
"""

import pytest

from eval.cases import Label
from eval.match import is_catch
from reviewer.findings import Finding

AUTHOR_OWNED = pytest.mark.xfail(strict=False, reason="author-owned")

LABEL = Label(buggy_file="pkg/mod.py", line_start=40, line_end=42, category="logic")


def finding(path, start, end=None):
    if start is not None and end is None:
        end = start
    return Finding(severity="WARNING", file=path, line_start=start, line_end=end, message="text")


SPEC_CASES = [
    # (#, path, start, end, tolerance, expected, why)
    (1, "pkg/mod.py", 41, None, 0, True, "inside range"),
    (2, "b/pkg/mod.py", 40, 42, 0, True, "prefix stripped (4.1)"),
    (3, "pkg/mod.py", 45, None, 0, False, "outside range"),
    (4, "pkg/mod.py", 45, None, 3, True, "within tolerance"),
    (5, "pkg/mod.py", 46, None, 3, False, "one line past tolerance"),
    (6, "pkg/other.py", 41, None, 10, False, "wrong file"),
    (7, None, 41, None, 10, False, "unlocated: no path"),
    (8, "pkg/mod.py", None, None, 10, False, "unlocated: no line"),
    (9, "pkg/mod.py", 42, 40, 0, True, "reversed range swapped (4.3)"),
    (10, "pkg/mod.py", 0, None, 100, False, "invalid line (4.3)"),
    (11, "pkg\\mod.py", 41, None, 0, True, "backslash normalized (4.1)"),
    (12, "pkg/mod.py", 30, 38, 1, False, "ends one line before the widened range (39)"),
    (13, "pkg/mod.py", 30, 39, 1, True, "touches widened range"),
]


@AUTHOR_OWNED
@pytest.mark.parametrize(
    "number,path,start,end,tolerance,expected,why", SPEC_CASES, ids=[f"case{c[0]}" for c in SPEC_CASES]
)
def test_spec_example(number, path, start, end, tolerance, expected, why):
    assert is_catch(finding(path, start, end), LABEL, tolerance) is expected, why


@AUTHOR_OWNED
def test_severity_is_ignored():
    for severity in ("CRITICAL", "WARNING", "NITPICK"):
        f = Finding(severity, "pkg/mod.py", 41, 41, "text")
        assert is_catch(f, LABEL, 0) is True


@AUTHOR_OWNED
def test_text_is_ignored():
    a = Finding("WARNING", "pkg/mod.py", 41, 41, "one description")
    b = Finding("WARNING", "pkg/mod.py", 41, 41, "a totally different description")
    assert is_catch(a, LABEL, 0) == is_catch(b, LABEL, 0)


@AUTHOR_OWNED
def test_monotone_in_tolerance():
    # MATCHING_SPEC §3.5: True at t implies True at every t' > t (0-20).
    for start in range(20, 65):
        for width in (0, 1, 3):
            f = finding("pkg/mod.py", start, start + width)
            results = [is_catch(f, LABEL, t) for t in range(0, 21)]
            first = results.index(True) if True in results else None
            if first is not None:
                assert all(results[first:]), (start, width, results)


@AUTHOR_OWNED
def test_deterministic():
    f = finding("pkg/mod.py", 41)
    assert len({is_catch(f, LABEL, 2) for _ in range(5)}) == 1


def test_stub_is_not_implemented_yet_or_returns_bool():
    # Not xfail: guards the contract either way. Before the owner's
    # implementation it must raise NotImplementedError; after, return a bool.
    try:
        value = is_catch(finding("pkg/mod.py", 41), LABEL, 0)
    except NotImplementedError:
        return
    assert isinstance(value, bool)
