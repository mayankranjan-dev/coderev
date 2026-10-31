"""
Fetch and cache the diffs the reviewer is shown (docs/EVAL_DESIGN.md §2.3-2.4).

Uses the git CLI only. For each case:

1. `git fetch --depth=2 <repo_url> <fix_sha>` into a bare repo under
   eval/cache/repos/ (gitignored). Fetches are spaced by --min-interval
   seconds and retried with exponential backoff, to stay polite to GitHub.
2. Load-time validation (§2.2): fix_sha is not a merge, parent_sha is its
   first parent, and for bug cases buggy_file exists at parent_sha and the
   labeled range is inside the file. A failure fails the run loudly.
3. The diff:
   - bug: reverse of the fix, `git diff <fix_sha> <parent_sha>`, so the
     reviewer sees a change that introduces the bug and the label's
     parent-file lines are on the + side.
   - control: forward, `git diff <parent_sha> <fix_sha>`.
   Both keep only *.py files, drop test files unless --include-tests, and use
   the same -U context setting.
4. The diff and metadata are cached as JSON under eval/cache/diffs/, so
   `--offline` reruns need no network at all.

Third-party code is only ever written under eval/cache/, which is gitignored.
Nothing here writes into tracked paths.

Case values reach git as argument lists (never a shell string), and the case
loader has already restricted SHAs to hex and repo URLs to github.com.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from eval.cases import DEFAULT_CASES_PATH, Case, CaseFileError, CaseSet, load_cases

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache"
# Qwen2.5-Coder family default (EVAL_DESIGN §6.1).
DEFAULT_CUTOFF = "2025-01-01"
# Bump when the filter or diff command changes, so old cache entries are not reused.
FILTER_VERSION = 1

_TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$")


class FetchError(RuntimeError):
    pass


@dataclass
class FetchedCase:
    case_id: str
    direction: str  # "reverse" (bug) | "forward" (control)
    context_lines: int
    files: list
    diff: str
    committer_date: str
    pre_cutoff: bool = False
    added_lines: int = 0
    removed_lines: int = 0
    from_cache: bool = field(default=False, compare=False)

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("from_cache")
        return data


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def select_files(paths: list, include_tests: bool = False) -> list:
    keep = [p for p in paths if p.endswith(".py")]
    if not include_tests:
        keep = [p for p in keep if not is_test_path(p)]
    return sorted(set(keep))


def repo_cache_name(repo_url: str) -> str:
    owner_repo = repo_url.rstrip("/").removesuffix(".git").split("github.com/", 1)[-1]
    return owner_repo.replace("/", "__") + ".git"


def _diff_counts(diff: str) -> tuple:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


class Fetcher:
    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        offline: bool = False,
        context_lines: int = 3,
        include_tests: bool = False,
        min_interval: float = 1.0,
        max_attempts: int = 3,
        backoff_base: float = 2.0,
        git: str = "git",
        timeout: float = 300.0,
        url_for: Optional[Callable[[str], str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if context_lines < 0:
            raise ValueError("context_lines must be >= 0")
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.context_lines = context_lines
        self.include_tests = include_tests
        self.min_interval = min_interval
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.git = git
        self.timeout = timeout
        # Tests point this at a local file:// repo; real runs use the case URL.
        self.url_for = url_for or (lambda url: url)
        self.sleep = sleep
        self.clock = clock
        self._last_fetch: Optional[float] = None
        self.network_fetches = 0

    # --- git plumbing -------------------------------------------------------

    def _run(self, args: list, cwd: Path) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        try:
            proc = subprocess.run(
                [self.git, "-c", "core.quotepath=off", *args],
                cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise FetchError(f"git {args[0]} timed out after {self.timeout:g}s") from exc
        except OSError as exc:
            raise FetchError(f"could not run git: {exc}") from exc
        if proc.returncode != 0:
            raise FetchError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def _repo_dir(self, case: Case) -> Path:
        path = self.cache_dir / "repos" / repo_cache_name(case.repo_url)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            self._run(["init", "--bare", "-q"], cwd=path)
        return path

    def _has_commit(self, repo: Path, sha: str) -> bool:
        try:
            self._run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
            return True
        except FetchError:
            return False

    def _throttle(self) -> None:
        if self._last_fetch is not None:
            wait = self.min_interval - (self.clock() - self._last_fetch)
            if wait > 0:
                self.sleep(wait)
        self._last_fetch = self.clock()

    def _fetch(self, case: Case, repo: Path) -> None:
        if self._has_commit(repo, case.fix_sha) and self._has_commit(repo, case.parent_sha):
            return
        if self.offline:
            raise FetchError(f"{case.id}: commits not in cache and --offline is set")
        url = self.url_for(case.repo_url)
        last_error = None
        for attempt in range(self.max_attempts):
            if attempt:
                self.sleep(self.backoff_base ** attempt)
            self._throttle()
            self.network_fetches += 1
            try:
                self._run(["fetch", "-q", "--depth=2", "--no-tags", url, case.fix_sha], cwd=repo)
                return
            except FetchError as exc:
                last_error = exc
        raise FetchError(f"{case.id}: fetch failed after {self.max_attempts} attempt(s): {last_error}")

    # --- validation (EVAL_DESIGN §2.2) ----------------------------------------

    def _validate(self, case: Case, repo: Path) -> None:
        parents = self._run(["rev-list", "--parents", "-n", "1", case.fix_sha], cwd=repo).split()[1:]
        if len(parents) > 1:
            raise FetchError(f"{case.id}: fix_sha is a merge commit ({len(parents)} parents)")
        if not parents or parents[0] != case.parent_sha:
            found = parents[0] if parents else "none"
            raise FetchError(f"{case.id}: parent_sha is not the first parent of fix_sha (first parent is {found})")
        if case.label is not None:
            path = case.label.buggy_file
            try:
                content = self._run(["show", f"{case.parent_sha}:{path}"], cwd=repo)
            except FetchError:
                raise FetchError(f"{case.id}: buggy_file {path!r} does not exist at parent_sha") from None
            line_count = len(content.splitlines())
            if case.label.line_end > line_count:
                raise FetchError(
                    f"{case.id}: line range {case.label.line_start}-{case.label.line_end} is outside "
                    f"{path} ({line_count} lines at parent_sha)"
                )

    # --- public -------------------------------------------------------------

    def cache_key(self, case: Case) -> str:
        raw = json.dumps(
            [case.repo_url, case.parent_sha, case.fix_sha, case.split, self.context_lines,
             self.include_tests, FILTER_VERSION],
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cache_path(self, case: Case) -> Path:
        return self.cache_dir / "diffs" / f"{case.id}-{self.cache_key(case)}.json"

    def get(self, case: Case, cutoff: str = DEFAULT_CUTOFF) -> FetchedCase:
        cache_path = self._cache_path(case)
        if cache_path.is_file():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched = FetchedCase(**data, from_cache=True)
            fetched.pre_cutoff = _before(fetched.committer_date, cutoff)
            return fetched

        repo = self._repo_dir(case)
        self._fetch(case, repo)
        self._validate(case, repo)

        changed = self._run(["diff", "--name-only", "--no-renames", case.parent_sha, case.fix_sha], cwd=repo)
        files = select_files([line for line in changed.splitlines() if line.strip()], self.include_tests)
        if not files:
            raise FetchError(f"{case.id}: no non-test .py files changed between parent_sha and fix_sha")
        if case.label is not None and case.label.buggy_file not in files:
            raise FetchError(f"{case.id}: buggy_file {case.label.buggy_file!r} is not among the changed files")

        bug = case.split == "bug"
        old, new = (case.fix_sha, case.parent_sha) if bug else (case.parent_sha, case.fix_sha)
        diff = self._run(
            ["diff", "--no-color", "--no-ext-diff", "--no-renames", f"-U{self.context_lines}", old, new, "--", *files],
            cwd=repo,
        )
        committer_date = self._run(["show", "-s", "--format=%cI", case.fix_sha], cwd=repo).strip()
        added, removed = _diff_counts(diff)
        fetched = FetchedCase(
            case_id=case.id,
            direction="reverse" if bug else "forward",
            context_lines=self.context_lines,
            files=files,
            diff=diff,
            committer_date=committer_date,
            pre_cutoff=_before(committer_date, cutoff),
            added_lines=added,
            removed_lines=removed,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(fetched.to_json()), encoding="utf-8")
        tmp.replace(cache_path)
        return fetched


def _before(committer_date: str, cutoff: str) -> bool:
    """True if the commit date is before the cutoff day (UTC date of the commit)."""
    try:
        when = datetime.fromisoformat(committer_date)
        limit = date.fromisoformat(cutoff)
    except ValueError:
        return True  # unknown dates are treated as contaminated, never as clean
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.date() < limit


def fetch_all(case_set: CaseSet, fetcher: Fetcher, cutoff: str = DEFAULT_CUTOFF) -> tuple:
    """Returns ({case_id: FetchedCase}, {case_id: error message})."""
    fetched, errors = {}, {}
    for case in case_set.cases:
        try:
            fetched[case.id] = fetcher.get(case, cutoff)
        except FetchError as exc:
            errors[case.id] = str(exc)
    return fetched, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and cache eval diffs.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--offline", action="store_true", help="use only cached diffs; never touch the network")
    parser.add_argument("--context", type=int, default=3, help="diff context lines (-U)")
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="contamination cutoff date (YYYY-MM-DD)")
    parser.add_argument("--min-interval", type=float, default=1.0, help="seconds between network fetches")
    args = parser.parse_args(argv)

    try:
        case_set = load_cases(args.cases)
    except CaseFileError as exc:
        print(exc, file=sys.stderr)
        return 2
    fetcher = Fetcher(
        cache_dir=Path(args.cache_dir), offline=args.offline, context_lines=args.context,
        include_tests=args.include_tests, min_interval=args.min_interval,
    )
    fetched, errors = fetch_all(case_set, fetcher, args.cutoff)
    pre = sum(1 for f in fetched.values() if f.pre_cutoff)
    print(f"fetched {len(fetched)} case(s) ({fetcher.network_fetches} network fetch(es)); "
          f"{pre} before cutoff {args.cutoff}; {len(errors)} error(s)")
    for case_id, message in sorted(errors.items()):
        print(f"ERROR {case_id}: {message}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
