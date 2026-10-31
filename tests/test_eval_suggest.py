"""eval/suggest.py with a mocked GitHub API. No network, no real repositories."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from eval import suggest
from eval.cases import DEFAULT_CASES_PATH

OWNER, REPO = "example-org", "synthetic"
BASE = "/repos/example-org/synthetic"


def sha(n: int) -> str:
    return f"{n:040x}"


def commit(n, message, date="2025-03-01T10:00:00Z", parents=1):
    return {
        "sha": sha(n),
        "commit": {"message": message, "committer": {"date": date}},
        "parents": [{"sha": sha(1000 + n + i)} for i in range(parents)],
    }


def detail(files):
    return {"files": [{"filename": f, "additions": a, "deletions": d} for f, a, d in files]}


class FakeGitHub:
    """Routes the endpoints suggest.py uses; counts calls and records headers."""

    def __init__(self, commits, details, private=False, link=None):
        self.commits, self.details, self.private, self.link = commits, details, private, link
        self.calls, self.auth_headers = [], []
        self.fail_with = None  # callable(request) -> Response | None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        self.auth_headers.append(request.headers.get("authorization"))
        if self.fail_with:
            response = self.fail_with(request)
            if response is not None:
                return response
        path = request.url.path
        if path == BASE:
            return httpx.Response(200, json={"private": self.private})
        if path == f"{BASE}/commits":
            page = int(request.url.params.get("page", 1))
            items = self.commits[page - 1] if page <= len(self.commits) else []
            headers = {"link": f'<https://api.github.com{BASE}/commits?page={len(self.commits)}>; rel="last"'}
            return httpx.Response(200, json=items, headers=headers if len(self.commits) > 1 else {})
        key = path.rsplit("/", 1)[-1]
        if key in self.details:
            return httpx.Response(200, json=self.details[key])
        return httpx.Response(404, json={"message": "Not Found"})


def run(tmp_path, gh, *extra, env_token=None, monkeypatch=None):
    out = tmp_path / "candidates.yaml"
    if "--output" in extra:  # a later --output overrides the default one below
        out = Path(extra[extra.index("--output") + 1])
    if monkeypatch is not None:
        if env_token:
            monkeypatch.setenv("GITHUB_TOKEN", env_token)
        else:
            monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = suggest.main(
        [f"https://github.com/{OWNER}/{REPO}", "--output", str(out), "--cache-dir", str(tmp_path / "cache"),
         "--min-interval-s", "0", *extra],
        transport=httpx.MockTransport(gh),
    )
    data = yaml.safe_load(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, data


@pytest.fixture
def gh():
    commits = [[
        commit(1, "Fix crash when the config is empty"),
        commit(2, "Add retry helper"),
        commit(3, "Fix parser bug", date="2024-12-31T23:59:59Z"),
        commit(4, "Merge pull request #9", parents=2),
        commit(5, "Fix typo in docs"),
        commit(6, "Fix big refactor regression"),
        commit(7, "Revert \"Add retry helper\""),
        commit(8, "Fix flaky test"),
        commit(9, "Refactor everything"),
    ]]
    details = {
        sha(1): detail([("pkg/config.py", 3, 2), ("README.md", 40, 0)]),
        sha(2): detail([("pkg/retry.py", 12, 0)]),
        sha(5): detail([("docs/index.md", 1, 1)]),
        sha(6): detail([("pkg/a.py", 40, 40)]),
        sha(8): detail([("tests/test_x.py", 2, 2)]),
        sha(9): detail([("pkg/a.py", 1, 0), ("pkg/b.py", 1, 0), ("pkg/c.py", 1, 0), ("pkg/d.py", 1, 0)]),
    }
    return FakeGitHub(commits, details)


def by_sha(data):
    return {entry["fix_sha"]: entry for entry in data["candidates"]}


def test_bug_fix_candidate_has_empty_label_fields_and_marker(tmp_path, gh):
    code, data = run(tmp_path, gh)
    assert code == 0
    entry = by_sha(data)[sha(1)]
    assert entry["suggested_kind"] == "bug-fix"
    assert entry["parent_sha"] == sha(1001)
    assert entry["link"] == f"https://github.com/{OWNER}/{REPO}/commit/{sha(1)}"
    assert entry["commit_message"] == "Fix crash when the config is empty"
    assert entry["python_files"] == ["pkg/config.py"]
    assert entry["changed_python_lines"] == 5  # the README's 40 lines are not Python
    for label_field in ("buggy_file", "line_start", "line_end", "category"):
        assert entry[label_field] is None
    assert entry["todo"] == "TODO(author): verify and label"
    assert "split" not in entry and "id" not in entry


def test_control_candidate_has_no_label_fields(tmp_path, gh):
    _, data = run(tmp_path, gh)
    entry = by_sha(data)[sha(2)]
    assert entry["suggested_kind"] == "control"
    assert "buggy_file" not in entry and "category" not in entry
    assert entry["todo"] == "TODO(author): verify and label"


def test_rejections(tmp_path, gh, capsys):
    _, data = run(tmp_path, gh)
    found = by_sha(data)
    assert set(found) == {sha(1), sha(2)}
    out = capsys.readouterr().out
    for reason in ("before cutoff", "merge or root commit", "no non-test .py file",
                   "too many changed lines", "revert commit", "too many .py files"):
        assert reason in out, reason


def test_size_limits_are_configurable(tmp_path, gh):
    _, data = run(tmp_path, gh, "--max-lines", "100", "--max-py-files", "4", "--max-bugs-per-repo", "9")
    assert {sha(6), sha(9)} <= set(by_sha(data))


def test_cutoff_is_inclusive_and_configurable(tmp_path, gh):
    _, data = run(tmp_path, gh, "--cutoff", "2025-03-01T10:00:00Z")
    assert sha(1) in by_sha(data)  # committed exactly at the cutoff instant: included
    later = tmp_path / "later.yaml"
    code, _ = run(tmp_path, gh, "--cutoff", "2025-03-01T10:00:01Z", "--output", str(later))
    assert code == 0
    assert yaml.safe_load(later.read_text(encoding="utf-8"))["candidates"] == []


def test_per_repo_caps(tmp_path):
    commits = [[commit(n, f"Fix problem {n}") for n in range(1, 8)]]
    details = {sha(n): detail([(f"pkg/m{n}.py", 1, 1)]) for n in range(1, 8)}
    _, data = run(tmp_path, FakeGitHub(commits, details), "--max-bugs-per-repo", "3")
    assert len(data["candidates"]) == 3


def test_oldest_first_walks_from_the_last_page(tmp_path):
    page1 = [commit(3, "Fix newest", date="2025-06-03T00:00:00Z"), commit(2, "Fix middle", date="2025-06-02T00:00:00Z")]
    page2 = [commit(1, "Fix oldest", date="2025-06-01T00:00:00Z")]
    details = {sha(n): detail([(f"pkg/m{n}.py", 1, 1)]) for n in (1, 2, 3)}
    _, data = run(tmp_path, FakeGitHub([page1, page2], details), "--max-bugs-per-repo", "2")
    assert [e["fix_sha"] for e in data["candidates"]] == [sha(1), sha(2)]
    _, newest = run(tmp_path, FakeGitHub([page1, page2], details), "--max-bugs-per-repo", "2", "--order", "newest",
                    "--output", str(tmp_path / "newest.yaml"), "--refresh")
    assert [e["fix_sha"] for e in newest["candidates"]] == [sha(3), sha(2)]


# --- output safety


def test_refuses_to_write_cases_yaml(tmp_path, gh, capsys):
    target = tmp_path / "cases.yaml"
    code = suggest.main([f"https://github.com/{OWNER}/{REPO}", "--output", str(target),
                         "--cache-dir", str(tmp_path / "cache")], transport=httpx.MockTransport(gh))
    assert code == 2
    assert not target.exists()
    assert gh.calls == []
    assert "refusing" in capsys.readouterr().err


def test_default_output_is_not_the_case_file():
    assert suggest.DEFAULT_OUTPUT.name == "candidates.yaml"
    assert suggest.DEFAULT_OUTPUT.resolve() != DEFAULT_CASES_PATH.resolve()
    with pytest.raises(suggest.SuggestError):
        suggest.assert_not_case_file(DEFAULT_CASES_PATH)


def test_candidate_file_cannot_be_loaded_as_a_case_file(tmp_path, gh):
    _, data = run(tmp_path, gh)
    assert "cases" not in data and "candidates" in data


def test_rerun_keeps_existing_entries(tmp_path, gh):
    _, first = run(tmp_path, gh)
    out = tmp_path / "candidates.yaml"
    edited = yaml.safe_load(out.read_text(encoding="utf-8"))
    edited["candidates"][0]["notes"] = "owner note"
    out.write_text(yaml.safe_dump(edited, sort_keys=False), encoding="utf-8")
    _, second = run(tmp_path, gh)
    assert len(second["candidates"]) == len(first["candidates"])
    assert second["candidates"][0]["notes"] == "owner note"


# --- network behavior


def test_second_run_is_served_from_the_cache(tmp_path, gh):
    run(tmp_path, gh)
    first_calls = len(gh.calls)
    assert first_calls > 0
    run(tmp_path, gh)
    assert len(gh.calls) == first_calls


def test_refresh_bypasses_the_cache(tmp_path, gh):
    run(tmp_path, gh)
    first_calls = len(gh.calls)
    run(tmp_path, gh, "--refresh")
    assert len(gh.calls) == 2 * first_calls


def test_works_without_a_token(tmp_path, gh, monkeypatch):
    code, _ = run(tmp_path, gh, monkeypatch=monkeypatch)
    assert code == 0
    assert set(gh.auth_headers) == {None}


def test_token_is_sent_but_never_printed(tmp_path, gh, monkeypatch, capsys):
    fake = "fake-token-for-test-0000"
    run(tmp_path, gh, env_token=fake, monkeypatch=monkeypatch)
    assert set(gh.auth_headers) == {f"Bearer {fake}"}
    captured = capsys.readouterr()
    assert fake not in captured.out + captured.err
    assert fake not in (tmp_path / "candidates.yaml").read_text(encoding="utf-8")
    assert not any(fake in p.read_text(encoding="utf-8") for p in (tmp_path / "cache").glob("*.json"))


def test_long_rate_limit_stops_with_partial_results(tmp_path, gh, capsys):
    served = {"n": 0}

    def limit_after_first_detail(request):
        if request.url.path.startswith(f"{BASE}/commits/"):
            served["n"] += 1
            if served["n"] > 1:
                return httpx.Response(403, headers={"x-ratelimit-remaining": "0",
                                                    "x-ratelimit-reset": str(4_000_000_000)})
        return None

    gh.fail_with = limit_after_first_detail
    code, data = run(tmp_path, gh, "--order", "newest")
    assert code == 3
    assert len(data["candidates"]) == 1
    assert "rate limit" in capsys.readouterr().err.lower()


def test_short_rate_limit_waits_and_retries(tmp_path):
    sleeps, state = [], {"limited": True}
    now = 1_000.0

    def handler(request):
        if state["limited"]:
            state["limited"] = False
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 5)})
        return httpx.Response(200, json={"ok": True})

    client = suggest.GitHubClient(cache_dir=tmp_path / "cache", min_interval_s=0, max_wait_s=60,
                                  transport=httpx.MockTransport(handler), sleep=sleeps.append, clock=lambda: now)
    assert client.get("/x")[0] == {"ok": True}
    assert sleeps and 5 <= sleeps[0] <= 7


def test_private_repo_is_skipped(tmp_path, gh, capsys):
    gh.private = True
    code, data = run(tmp_path, gh)
    assert code == 1
    assert data["candidates"] == []
    assert "private" in capsys.readouterr().err


def test_non_github_url_is_a_usage_error(tmp_path, capsys):
    code = suggest.main(["https://gitlab.com/a/b", "--output", str(tmp_path / "c.yaml")])
    assert code == 2


@pytest.mark.parametrize("message,expected", [
    ("Fix crash on empty input", True),
    ("fixes #12", True),
    ("Resolve BUG in parser", True),
    ("Fixed regression", True),
    ("patch vulnerability", True),
    ("bugfix: cache key", True),
    ("Add fixture for tests", False),
    ("prefix handling", False),
    ("Refactor module layout", False),
    ("Add feature", False),
])
def test_fix_pattern(message, expected):
    assert suggest.is_fix_message(message) is expected


def test_cache_files_contain_only_api_json(tmp_path, gh):
    run(tmp_path, gh)
    for path in (tmp_path / "cache").glob("*.json"):
        assert set(json.loads(path.read_text(encoding="utf-8"))) == {"body", "link"}
