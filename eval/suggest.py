"""
Candidate finder: shortlist commits for the owner to label. It DECIDES NOTHING.

    python -m eval.suggest https://github.com/owner/repo [more URLs] \
        [--max-py-files 3] [--max-lines 50] [--order oldest|newest]

For each public repo it lists commits dated on or after the contamination
cutoff (docs/EVAL_DESIGN.md §6.1, default 2025-01-01) through the GitHub REST
API and keeps two kinds of draft entry in eval/candidates.yaml:

- bug-fix candidates: the message matches FIX_PATTERN (fix, bug, regression,
  crash, vulnerability);
- control candidates: the message does not match it.

Both must have exactly one parent, touch at least one non-test .py file, touch
at most --max-py-files non-test .py files, and change at most --max-lines
lines in those files. The defaults are the "suggested" values in
docs/eval/LABELING_RULES.md §4.2, not decisions.

What it never does (.kiro/steering/project.md §7):
- It never writes eval/cases.yaml (the output path is refused if it is that
  file or any file named cases.yaml).
- It never picks a buggy file, line range, category, or split. Those fields are
  left empty with a "TODO(author): verify and label" marker. A message that
  mentions "fix" is a search filter, not evidence that the commit is a bug.

Network behavior:
- Every response is cached under eval/cache/github_api/ (gitignored). A re-run
  reads the cache and costs no API calls. --refresh bypasses it.
- GITHUB_TOKEN is read from the process environment if set (5000 requests/hour
  instead of 60). It is only sent to api.github.com and never printed. This
  module does not read .env; set the variable in your shell.
- The remaining quota is tracked from the X-RateLimit headers. A short wait for
  the reset is taken (--max-wait-s); a longer one stops the run, keeps what was
  found, and exits with code 3.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx
import yaml

from eval.cases import DEFAULT_CASES_PATH
from eval.fetch import DEFAULT_CACHE_DIR, DEFAULT_CUTOFF, is_test_path

API_ROOT = "https://api.github.com"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "candidates.yaml"
DEFAULT_API_CACHE = DEFAULT_CACHE_DIR / "github_api"
TODO_MARKER = "TODO(author): verify and label"
# The commit-detail endpoint lists at most 300 files; past that the list is
# incomplete, so such commits are rejected rather than measured wrongly.
GITHUB_FILE_LIST_LIMIT = 300

FIX_PATTERN = re.compile(
    r"\b(fix(e[sd])?|bug(s|fix(es)?)?|regression|crash(es|ed|ing)?|vulnerabilit(y|ies))\b", re.IGNORECASE
)
_REVERT = re.compile(r"^\s*revert\b", re.IGNORECASE)
_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_LAST_PAGE = re.compile(r'[?&]page=(\d+)[^>]*>;\s*rel="last"')


class SuggestError(Exception):
    pass


class RateLimited(SuggestError):
    def __init__(self, reset_at: Optional[float]):
        self.reset_at = reset_at
        when = datetime.fromtimestamp(reset_at, timezone.utc).strftime("%H:%M:%SZ") if reset_at else "unknown"
        super().__init__(f"GitHub API rate limit reached; it resets at {when}")


def parse_repo_url(url: str) -> tuple:
    match = _REPO_URL.match(url.strip())
    if not match:
        raise SuggestError(f"not a github.com repository URL: {url!r}")
    return match.group(1), match.group(2)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class GitHubClient:
    """GET-only GitHub API client with a disk cache and rate-limit handling."""

    def __init__(
        self,
        cache_dir: Path = DEFAULT_API_CACHE,
        token: Optional[str] = None,
        refresh: bool = False,
        min_interval_s: float = 0.5,
        max_wait_s: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 3,
    ):
        self.cache_dir = Path(cache_dir)
        self.refresh = refresh
        self.min_interval_s = min_interval_s
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self.clock = clock
        self.max_attempts = max_attempts
        self.requests_made = 0
        self.cache_hits = 0
        self.remaining: Optional[int] = None
        self.reset_at: Optional[float] = None
        self._last_request = 0.0
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "coderev-bot-eval-suggest",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=API_ROOT, headers=headers, timeout=30.0, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _cache_file(self, path: str, params: dict) -> Path:
        key = path + "?" + urlencode(sorted(params.items()))
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:40] + ".json")

    def _wait_for_quota(self) -> None:
        if self.remaining is not None and self.remaining <= 0 and self.reset_at:
            wait = self.reset_at - self.clock() + 1.0
            if wait > self.max_wait_s:
                raise RateLimited(self.reset_at)
            if wait > 0:
                self.sleep(wait)
            self.remaining = None
        gap = self.min_interval_s - (self.clock() - self._last_request)
        if gap > 0:
            self.sleep(gap)

    def _note_headers(self, response: httpx.Response) -> None:
        remaining, reset = response.headers.get("x-ratelimit-remaining"), response.headers.get("x-ratelimit-reset")
        if remaining is not None and remaining.isdigit():
            self.remaining = int(remaining)
        if reset is not None and reset.isdigit():
            self.reset_at = float(reset)

    def get(self, path: str, params: Optional[dict] = None) -> tuple:
        """Return (parsed JSON, Link header or ''). Only 200 responses are cached."""
        params = params or {}
        cache_file = self._cache_file(path, params)
        if not self.refresh and cache_file.is_file():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self.cache_hits += 1
            return cached["body"], cached.get("link", "")

        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_quota()
            self._last_request = self.clock()
            self.requests_made += 1
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
                self.sleep(min(2 ** attempt, 10))
                continue
            self._note_headers(response)
            if response.status_code == 200:
                body, link = response.json(), response.headers.get("link", "")
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"body": body, "link": link}), encoding="utf-8")
                return body, link
            exhausted = response.status_code in (403, 429) and (
                response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
            )
            if exhausted:
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    self.remaining, self.reset_at = 0, self.clock() + int(retry_after)
                self._wait_for_quota()  # raises RateLimited when the wait is too long
                continue
            if response.status_code in (502, 503, 504):
                last_error = f"HTTP {response.status_code}"
                self.sleep(min(2 ** attempt, 10))
                continue
            raise SuggestError(f"GET {path} failed with HTTP {response.status_code}")
        raise SuggestError(f"GET {path} failed after {self.max_attempts} attempts ({last_error})")


@dataclass
class Settings:
    cutoff: str = DEFAULT_CUTOFF
    max_py_files: int = 3
    max_lines: int = 50
    max_bugs_per_repo: int = 5
    max_controls_per_repo: int = 5
    max_scan: int = 300
    max_detail_calls: int = 60
    order: str = "oldest"


@dataclass
class RepoResult:
    repo_url: str
    candidates: list = field(default_factory=list)
    rejections: dict = field(default_factory=dict)
    scanned: int = 0
    stopped_by: Optional[str] = None  # a RateLimited message, if the run was cut short

    def reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1


def is_fix_message(message: str) -> bool:
    return bool(FIX_PATTERN.search(message))


def iter_commits(client: GitHubClient, owner: str, repo: str, settings: Settings):
    """Yield list-endpoint commit objects since the cutoff, oldest first or newest first."""
    path = f"/repos/{owner}/{repo}/commits"
    since = parse_time(settings.cutoff).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"since": since, "per_page": 100}
    first, link = client.get(path, {**base, "page": 1})
    match = _LAST_PAGE.search(link or "")
    last_page = int(match.group(1)) if match else 1
    pages = range(last_page, 0, -1) if settings.order == "oldest" else range(1, last_page + 1)
    for page in pages:
        items = first if page == 1 else client.get(path, {**base, "page": page})[0]
        yield from (reversed(items) if settings.order == "oldest" else items)


def python_change(detail: dict) -> Optional[dict]:
    """Non-test .py files and their changed-line total, or None if the file list is unusable."""
    files = detail.get("files")
    if not isinstance(files, list) or len(files) >= GITHUB_FILE_LIST_LIMIT:
        return None
    py = [f for f in files if str(f.get("filename", "")).endswith(".py")]
    source = [f for f in py if not is_test_path(f["filename"])]
    return {
        "files": sorted(f["filename"] for f in source),
        "lines": sum(int(f.get("additions", 0)) + int(f.get("deletions", 0)) for f in source),
        "touches_test_files": len(py) != len(source),
    }


def draft_entry(kind: str, owner: str, repo: str, item: dict, change: dict) -> dict:
    sha = item["sha"]
    message = (item.get("commit", {}).get("message") or "").strip()
    entry = {
        "suggested_id": re.sub(r"[^a-z0-9._-]", "-", f"{repo}-{sha[:7]}".lower()),
        "suggested_kind": kind,
        "repo_url": f"https://github.com/{owner}/{repo}",
        "parent_sha": item["parents"][0]["sha"],
        "fix_sha": sha,
        "committer_date": item["commit"]["committer"]["date"],
        "commit_message": message[:500],
        "link": f"https://github.com/{owner}/{repo}/commit/{sha}",
        "python_files": change["files"],
        "changed_python_lines": change["lines"],
        "touches_test_files": change["touches_test_files"],
    }
    if kind == "bug-fix":
        entry.update(buggy_file=None, line_start=None, line_end=None, category=None)
    entry["todo"] = TODO_MARKER
    return entry


def find_candidates(client: GitHubClient, repo_url: str, settings: Settings) -> RepoResult:
    owner, repo = parse_repo_url(repo_url)
    result = RepoResult(repo_url=f"https://github.com/{owner}/{repo}")
    meta, _ = client.get(f"/repos/{owner}/{repo}")
    if meta.get("private"):
        raise SuggestError(f"{result.repo_url} is private; only public repositories are supported")

    cutoff = parse_time(settings.cutoff)
    bugs = controls = detail_calls = 0
    try:
        for item in iter_commits(client, owner, repo, settings):
            if result.scanned >= settings.max_scan:
                result.reject("scan limit reached")
                break
            if bugs >= settings.max_bugs_per_repo and controls >= settings.max_controls_per_repo:
                break
            result.scanned += 1
            message = item.get("commit", {}).get("message") or ""
            if len(item.get("parents", [])) != 1:
                result.reject("merge or root commit")
                continue
            if parse_time(item["commit"]["committer"]["date"]) < cutoff:
                result.reject("before cutoff")
                continue
            fix = is_fix_message(message)
            if fix and bugs >= settings.max_bugs_per_repo:
                continue
            if not fix and (controls >= settings.max_controls_per_repo or _REVERT.match(message)):
                if _REVERT.match(message):
                    result.reject("revert commit")
                continue
            if detail_calls >= settings.max_detail_calls:
                result.reject("detail-call budget exhausted")
                break
            detail_calls += 1
            detail, _ = client.get(f"/repos/{owner}/{repo}/commits/{item['sha']}")
            change = python_change(detail)
            if change is None:
                result.reject("file list incomplete")
            elif not change["files"]:
                result.reject("no non-test .py file")
            elif len(change["files"]) > settings.max_py_files:
                result.reject("too many .py files")
            elif change["lines"] > settings.max_lines:
                result.reject("too many changed lines")
            else:
                kind = "bug-fix" if fix else "control"
                result.candidates.append(draft_entry(kind, owner, repo, item, change))
                bugs, controls = bugs + (kind == "bug-fix"), controls + (kind == "control")
    except RateLimited as exc:
        result.stopped_by = str(exc)
    return result


def assert_not_case_file(path: Path) -> None:
    if path.name == "cases.yaml" or path.resolve() == DEFAULT_CASES_PATH.resolve():
        raise SuggestError("refusing to write a case file: labels are the owner's work, candidates go elsewhere")


def write_candidates(path: Path, results: list, settings: Settings) -> int:
    """Merge new drafts into the file (existing entries are kept) and return how many were added."""
    assert_not_case_file(path)
    existing = []
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        existing = list(loaded.get("candidates") or [])
    known = {entry.get("fix_sha") for entry in existing}
    added = [c for r in results for c in r.candidates if c["fix_sha"] not in known]
    document = {
        "version": 1,
        "note": (
            "DRAFTS ONLY. Generated by eval/suggest.py from commit messages and sizes. Nothing here is labeled. "
            "Verify each commit, then copy the fields you accept into eval/cases.yaml by hand."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cutoff": settings.cutoff,
        "candidates": existing + added,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    return len(added)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft labeling candidates from public GitHub repos.")
    parser.add_argument("repos", nargs="*", help="https://github.com/owner/repo")
    parser.add_argument("--repos-file", help="text file with one repo URL per line (# comments allowed)")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="earliest committer date, default from the eval design")
    parser.add_argument("--max-py-files", type=int, default=Settings.max_py_files, help="N: non-test .py files")
    parser.add_argument("--max-lines", type=int, default=Settings.max_lines, help="M: changed lines in those files")
    parser.add_argument("--max-bugs-per-repo", type=int, default=Settings.max_bugs_per_repo)
    parser.add_argument("--max-controls-per-repo", type=int, default=Settings.max_controls_per_repo)
    parser.add_argument("--max-scan", type=int, default=Settings.max_scan, help="commits examined per repo")
    parser.add_argument("--max-detail-calls", type=int, default=Settings.max_detail_calls,
                        help="per-commit API calls per repo (the main cost without a token)")
    parser.add_argument("--order", choices=["oldest", "newest"], default=Settings.order)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--cache-dir", default=str(DEFAULT_API_CACHE))
    parser.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    parser.add_argument("--max-wait-s", type=float, default=60.0, help="longest rate-limit wait before stopping")
    parser.add_argument("--min-interval-s", type=float, default=0.5, help="pause between uncached API requests")
    return parser


def read_repo_list(args) -> list:
    urls = list(args.repos)
    if args.repos_file:
        for line in Path(args.repos_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                urls.append(line)
    return list(dict.fromkeys(urls))


def main(argv=None, transport: Optional[httpx.BaseTransport] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        urls = read_repo_list(args)
        if not urls:
            raise SuggestError("give at least one repository URL")
        for url in urls:
            parse_repo_url(url)
        output = Path(args.output)
        assert_not_case_file(output)
        parse_time(args.cutoff)
    except (SuggestError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    settings = Settings(
        cutoff=args.cutoff, max_py_files=args.max_py_files, max_lines=args.max_lines,
        max_bugs_per_repo=args.max_bugs_per_repo, max_controls_per_repo=args.max_controls_per_repo,
        max_scan=args.max_scan, max_detail_calls=args.max_detail_calls, order=args.order,
    )
    token = os.environ.get("GITHUB_TOKEN") or None
    print(f"GitHub API: {'authenticated (GITHUB_TOKEN set)' if token else 'unauthenticated (60 requests/hour)'}")
    client = GitHubClient(Path(args.cache_dir), token, args.refresh, min_interval_s=args.min_interval_s,
                          max_wait_s=args.max_wait_s, transport=transport)
    results, exit_code = [], 0
    try:
        for url in urls:
            try:
                result = find_candidates(client, url, settings)
            except SuggestError as exc:
                print(f"{url}: skipped: {exc}", file=sys.stderr)
                exit_code = exit_code or 1
                continue
            results.append(result)
            bugs = sum(c["suggested_kind"] == "bug-fix" for c in result.candidates)
            print(f"{result.repo_url}: scanned {result.scanned}, {bugs} bug-fix and "
                  f"{len(result.candidates) - bugs} control candidate(s)")
            for reason, count in sorted(result.rejections.items()):
                print(f"    rejected {count}: {reason}")
            if result.stopped_by:
                print(f"    stopped early: {result.stopped_by}", file=sys.stderr)
                exit_code = 3
                break
    finally:
        client.close()
    added = write_candidates(output, results, settings)
    print(f"wrote {added} new draft(s) to {output} ({client.requests_made} API request(s), "
          f"{client.cache_hits} cache hit(s)). Every entry still needs your review and label.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
