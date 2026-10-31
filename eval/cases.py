"""
Case file schema and validating loader (eval/cases.yaml).

The case file is label data and is written by the owner (see
docs/eval/LABELING_RULES.md and eval/LABELING.md). This module only reads
and checks it; it never writes cases.

Schema
------

```yaml
version: 1                    # required, must be 1
categories: [logic, ...]      # optional; if present, every bug category must be in it
cases:
  - id: short-stable-id       # required, unique, [a-z0-9][a-z0-9._-]{0,63}
    repo_url: https://github.com/<owner>/<repo>   # required
    parent_sha: <40 hex>      # required; first parent of fix_sha
    fix_sha: <40 hex>         # required; for controls, the control commit
    split: bug                # required: bug | control
    buggy_file: pkg/mod.py    # bug only: path at parent_sha, relative, *.py
    line_start: 40            # bug only: 1-based, inclusive, at parent_sha
    line_end: 42              # bug only: >= line_start
    category: logic           # bug only
    notes: free text          # optional
```

Controls must not have buggy_file, line_start, line_end, or category. One
contiguous range per bug (LABELING_RULES 4.1 suggested default); a list of
ranges is rejected with a message saying so.

Checks that need the repository (parent is the first parent, file exists at
parent_sha, range inside the file, committer date after the cutoff) are done
by eval/fetch.py, which has the git checkout.

All problems in a file are collected and reported together, each prefixed
with the case's index and id, so one run shows every mistake.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
SPLITS = ("bug", "control")

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$")

REQUIRED = ("id", "repo_url", "parent_sha", "fix_sha", "split")
LABEL_FIELDS = ("buggy_file", "line_start", "line_end", "category")
OPTIONAL = ("notes",)
KNOWN = set(REQUIRED) | set(LABEL_FIELDS) | set(OPTIONAL)


class CaseFileError(ValueError):
    def __init__(self, path, problems):
        self.path = str(path)
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{self.path}: {len(self.problems)} problem(s):\n{lines}")


@dataclass(frozen=True)
class Label:
    """Field names match docs/eval/MATCHING_SPEC.md §2."""

    buggy_file: str
    line_start: int
    line_end: int
    category: str


@dataclass(frozen=True)
class Case:
    id: str
    repo_url: str
    parent_sha: str
    fix_sha: str
    split: str
    label: Optional[Label] = None
    notes: Optional[str] = None

    @property
    def is_bug(self) -> bool:
        return self.split == "bug"


@dataclass(frozen=True)
class CaseSet:
    path: str
    cases: tuple
    categories: Optional[tuple]
    blob_hash: str  # git blob hash of the bytes on disk (`git hash-object --no-filters <path>`)

    @property
    def bugs(self) -> list:
        return [c for c in self.cases if c.split == "bug"]

    @property
    def controls(self) -> list:
        return [c for c in self.cases if c.split == "control"]

    def by_id(self) -> dict:
        return {c.id: c for c in self.cases}


def git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_case(index: int, raw, categories, seen_ids, problems) -> Optional[Case]:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping, got {type(raw).__name__}")
        return None
    case_id = raw.get("id")
    if isinstance(case_id, str):
        where = f"cases[{index}] (id={case_id})"
    before = len(problems)

    for key in sorted(set(raw) - KNOWN):
        hint = " (multiple ranges per bug are not supported in v1)" if key in ("ranges", "lines") else ""
        problems.append(f"{where}: unknown field '{key}'{hint}")
    for key in REQUIRED:
        if raw.get(key) in (None, ""):
            problems.append(f"{where}: missing required field '{key}'")

    if isinstance(case_id, str):
        if not _ID.match(case_id):
            problems.append(f"{where}: id must match {_ID.pattern}")
        elif case_id in seen_ids:
            problems.append(f"{where}: duplicate id (first used at cases[{seen_ids[case_id]}])")
        else:
            seen_ids[case_id] = index
    elif case_id is not None:
        problems.append(f"{where}: id must be a string")

    repo_url = raw.get("repo_url")
    if repo_url is not None and not (isinstance(repo_url, str) and _REPO.match(repo_url)):
        problems.append(f"{where}: repo_url must look like https://github.com/<owner>/<repo>, got {repo_url!r}")

    for key in ("parent_sha", "fix_sha"):
        value = raw.get(key)
        if value is not None and not (isinstance(value, str) and _SHA.match(value)):
            problems.append(f"{where}: {key} must be a full 40-character lowercase hex SHA, got {value!r}")
    if raw.get("parent_sha") and raw.get("parent_sha") == raw.get("fix_sha"):
        problems.append(f"{where}: parent_sha and fix_sha are the same commit")

    split = raw.get("split")
    if split is not None and split not in SPLITS:
        problems.append(f"{where}: split must be one of {SPLITS}, got {split!r}")

    label = None
    if split == "bug":
        for key in LABEL_FIELDS:
            if raw.get(key) in (None, ""):
                problems.append(f"{where}: bug case is missing '{key}'")
        path = raw.get("buggy_file")
        if path is not None:
            if not isinstance(path, str):
                problems.append(f"{where}: buggy_file must be a string")
            else:
                normalized = path.replace("\\", "/")
                if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                    problems.append(f"{where}: buggy_file must be relative to the repo root, got {path!r}")
                if ".." in normalized.split("/"):
                    problems.append(f"{where}: buggy_file must not contain '..'")
                if normalized.startswith(("a/", "b/")):
                    problems.append(f"{where}: buggy_file must not keep the diff's a/ or b/ prefix")
                if not normalized.endswith(".py"):
                    problems.append(f"{where}: buggy_file must be a .py file (EVAL_DESIGN 2.4)")
        start, end = raw.get("line_start"), raw.get("line_end")
        for key, value in (("line_start", start), ("line_end", end)):
            if value is not None and not (_is_int(value) and value >= 1):
                if isinstance(value, list):
                    problems.append(f"{where}: {key} must be one integer (multiple ranges are not supported in v1)")
                else:
                    problems.append(f"{where}: {key} must be an integer >= 1, got {value!r}")
        if _is_int(start) and _is_int(end) and end < start:
            problems.append(f"{where}: line_end ({end}) is before line_start ({start})")
        category = raw.get("category")
        if category is not None:
            if not isinstance(category, str):
                problems.append(f"{where}: category must be a string")
            elif categories is not None and category not in categories:
                problems.append(f"{where}: category {category!r} is not in the file's categories list")
        if len(problems) == before:
            label = Label(path.replace("\\", "/"), start, end, category)
    elif split == "control":
        for key in LABEL_FIELDS:
            if raw.get(key) is not None:
                problems.append(f"{where}: control case must not have '{key}'")

    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        problems.append(f"{where}: notes must be a string")

    if len(problems) != before:
        return None
    return Case(
        id=case_id,
        repo_url=repo_url.rstrip("/"),
        parent_sha=raw["parent_sha"],
        fix_sha=raw["fix_sha"],
        split=split,
        label=label,
        notes=notes,
    )


def load_cases(path=DEFAULT_CASES_PATH) -> CaseSet:
    path = Path(path)
    if not path.is_file():
        raise CaseFileError(path, [
            "file not found. Copy eval/cases.example.yaml to eval/cases.yaml and label real "
            "cases following eval/LABELING.md."
        ])
    data_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(data_bytes.decode("utf-8"))
    except UnicodeDecodeError:
        raise CaseFileError(path, ["file is not UTF-8"]) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise CaseFileError(path, [f"invalid YAML{where}: {getattr(exc, 'problem', exc)}"]) from None

    problems = []
    if not isinstance(data, dict):
        raise CaseFileError(path, ["top level must be a mapping with 'version' and 'cases'"])
    for key in sorted(set(data) - {"version", "categories", "cases"}):
        problems.append(f"unknown top-level field '{key}'")
    if data.get("version") != 1:
        problems.append(f"version must be 1, got {data.get('version')!r}")

    categories = data.get("categories")
    if categories is not None:
        if not (isinstance(categories, list) and all(isinstance(c, str) and c for c in categories)):
            problems.append("categories must be a list of non-empty strings")
            categories = None
        else:
            categories = tuple(categories)

    raw_cases = data.get("cases")
    cases = []
    if not isinstance(raw_cases, list) or not raw_cases:
        problems.append("cases must be a non-empty list")
    else:
        seen = {}
        for index, raw in enumerate(raw_cases):
            case = _check_case(index, raw, categories, seen, problems)
            if case is not None:
                cases.append(case)

    if problems:
        raise CaseFileError(path, problems)
    return CaseSet(str(path), tuple(cases), categories, git_blob_hash(data_bytes))
