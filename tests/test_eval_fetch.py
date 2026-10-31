"""
Fetcher tests against a throwaway local git repo served over file://.
No network: url_for points every case at the local repo.
"""

import subprocess
from pathlib import Path

import pytest

from eval.cases import Case, Label
from eval.fetch import FetchError, Fetcher, fetch_all, is_test_path, repo_cache_name, select_files

ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]

BUGGY = "def total(items):\n    t = 0\n    for i in range(len(items) - 1):\n        t += items[i]\n    return t\n"
FIXED = "def total(items):\n    t = 0\n    for i in range(len(items)):\n        t += items[i]\n    return t\n"


def git(repo, *args):
    return subprocess.run(["git", *ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, message):
    for name, text in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    repo = tmp_path_factory.mktemp("upstream")
    git(repo, "init", "-q")
    base = commit(repo, {"pkg/calc.py": BUGGY, "README.md": "x\n", "tests/test_calc.py": "old\n"}, "base")
    fix = commit(repo, {"pkg/calc.py": FIXED, "README.md": "y\n", "tests/test_calc.py": "new\n"}, "fix")
    feature = commit(repo, {"pkg/extra.py": "VALUE = 1\n"}, "feature")
    # A merge commit: branch off base, then merge.
    git(repo, "checkout", "-q", "-b", "side", base)
    side = commit(repo, {"pkg/side.py": "S = 1\n"}, "side")
    git(repo, "checkout", "-q", "-")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", side)
    merge = git(repo, "rev-parse", "HEAD")
    return {"path": repo, "base": base, "fix": fix, "feature": feature, "merge": merge, "side": side}


def make_fetcher(upstream, tmp_path, **kwargs):
    url = Path(upstream["path"]).as_uri()
    kwargs.setdefault("min_interval", 0)
    return Fetcher(cache_dir=tmp_path / "cache", url_for=lambda _url: url, **kwargs)


def bug_case(up, **kw):
    label = kw.pop("label", Label("pkg/calc.py", 3, 3, "logic"))
    return Case(id=kw.pop("id", "calc-off-by-one"), repo_url="https://github.com/example-org/example-repo",
                parent_sha=kw.pop("parent", up["base"]), fix_sha=kw.pop("fix", up["fix"]), split="bug", label=label)


def control_case(up, **kw):
    return Case(id="extra-feature", repo_url="https://github.com/example-org/example-repo",
                parent_sha=up["fix"], fix_sha=up["feature"], split="control")


def test_bug_case_gets_reverse_diff_of_py_non_test_files(upstream, tmp_path):
    fetched = make_fetcher(upstream, tmp_path).get(bug_case(upstream))
    assert fetched.direction == "reverse"
    assert fetched.files == ["pkg/calc.py"]
    assert "+    for i in range(len(items) - 1):" in fetched.diff  # buggy line on the + side
    assert "-    for i in range(len(items)):" in fetched.diff
    assert "README" not in fetched.diff and "tests/" not in fetched.diff
    assert (fetched.added_lines, fetched.removed_lines) == (1, 1)


def test_reverse_diff_plus_side_uses_parent_line_numbers(upstream, tmp_path):
    from reviewer.diffutil import parse_unified_diff

    fetched = make_fetcher(upstream, tmp_path, context_lines=0).get(bug_case(upstream))
    [fd] = parse_unified_diff(fetched.diff)
    assert fd.path == "pkg/calc.py"
    assert fd.added_line_numbers() == {3}  # the labeled parent-file line


def test_control_case_gets_forward_diff(upstream, tmp_path):
    fetched = make_fetcher(upstream, tmp_path).get(control_case(upstream))
    assert fetched.direction == "forward"
    assert "+VALUE = 1" in fetched.diff


def test_include_tests_option(upstream, tmp_path):
    fetched = make_fetcher(upstream, tmp_path, include_tests=True).get(bug_case(upstream))
    assert fetched.files == ["pkg/calc.py", "tests/test_calc.py"]


def test_context_lines_setting(upstream, tmp_path):
    wide = make_fetcher(upstream, tmp_path, context_lines=5).get(bug_case(upstream))
    narrow = make_fetcher(upstream, tmp_path / "n", context_lines=0).get(bug_case(upstream))
    assert len(wide.diff) > len(narrow.diff)


def test_cache_enables_offline_rerun(upstream, tmp_path):
    first = make_fetcher(upstream, tmp_path)
    original = first.get(bug_case(upstream))
    assert first.network_fetches == 1

    offline = Fetcher(cache_dir=tmp_path / "cache", offline=True, git="git-must-not-run")
    again = offline.get(bug_case(upstream))
    assert again.from_cache and again.diff == original.diff
    assert offline.network_fetches == 0


def test_offline_without_cache_fails(upstream, tmp_path):
    with pytest.raises(FetchError, match="--offline"):
        make_fetcher(upstream, tmp_path, offline=True).get(bug_case(upstream))


def test_repo_is_fetched_once_for_two_cases(upstream, tmp_path):
    fetcher = make_fetcher(upstream, tmp_path)
    fetcher.get(bug_case(upstream))
    fetcher.get(bug_case(upstream, id="same-commits-other-id"))
    assert fetcher.network_fetches == 1


def test_merge_commit_rejected(upstream, tmp_path):
    case = bug_case(upstream, fix=upstream["merge"], parent=upstream["feature"],
                    label=Label("pkg/side.py", 1, 1, "logic"))
    with pytest.raises(FetchError, match="merge commit"):
        make_fetcher(upstream, tmp_path).get(case)


def test_wrong_parent_rejected(upstream, tmp_path):
    case = bug_case(upstream, parent=upstream["feature"])
    with pytest.raises(FetchError, match="not the first parent"):
        make_fetcher(upstream, tmp_path).get(case)


def test_missing_buggy_file_rejected(upstream, tmp_path):
    case = bug_case(upstream, label=Label("pkg/nope.py", 1, 1, "logic"))
    with pytest.raises(FetchError, match="does not exist at parent_sha"):
        make_fetcher(upstream, tmp_path).get(case)


def test_range_outside_file_rejected(upstream, tmp_path):
    case = bug_case(upstream, label=Label("pkg/calc.py", 4, 99, "logic"))
    with pytest.raises(FetchError, match="outside pkg/calc.py \\(5 lines"):
        make_fetcher(upstream, tmp_path).get(case)


def test_contamination_cutoff_flag(upstream, tmp_path):
    fetcher = make_fetcher(upstream, tmp_path)
    assert fetcher.get(bug_case(upstream), cutoff="2999-01-01").pre_cutoff
    assert not fetcher.get(bug_case(upstream), cutoff="2000-01-01").pre_cutoff  # from cache, re-evaluated


def test_fetch_all_collects_errors_without_stopping(upstream, tmp_path):
    from eval.cases import CaseSet

    good = bug_case(upstream)
    bad = bug_case(upstream, id="bad", label=Label("pkg/nope.py", 1, 1, "logic"))
    fetched, errors = fetch_all(CaseSet("x", (good, bad), None, "h"), make_fetcher(upstream, tmp_path))
    assert list(fetched) == ["calc-off-by-one"] and list(errors) == ["bad"]


def test_throttle_and_backoff(tmp_path):
    sleeps = []
    clock = iter([0.0, 0.0, 0.1, 0.1, 0.2, 0.2, 5.0, 5.0]).__next__
    fetcher = Fetcher(cache_dir=tmp_path, min_interval=1.0, max_attempts=3, backoff_base=2.0,
                      sleep=sleeps.append, clock=clock, url_for=lambda u: "file:///does/not/exist")
    calls = []

    def fake_run(args, cwd):
        calls.append(args[0])
        if args[0] == "fetch":
            raise FetchError("unreachable")
        if args[0] == "cat-file":
            raise FetchError("missing")
        return ""

    fetcher._run = fake_run
    case = Case("c", "https://github.com/o/r", "a" * 40, "b" * 40, "control")
    with pytest.raises(FetchError, match="after 3 attempt"):
        fetcher._fetch(case, tmp_path)
    assert calls.count("fetch") == 3
    # Backoff sleeps of 2 and 4 seconds between attempts, plus throttle waits.
    assert 2.0 in sleeps and 4.0 in sleeps
    assert any(0 < s <= 1.0 for s in sleeps)


def test_helpers():
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/test_x.py") and is_test_path("a/x_test.py")
    assert not is_test_path("pkg/contest.py")
    assert select_files(["a.py", "b.md", "tests/t.py", "a.py"]) == ["a.py"]
    assert repo_cache_name("https://github.com/Own/Repo.git") == "Own__Repo.git"
