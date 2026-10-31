"""
Unified diff parsing, hunk-to-file line mapping, and input chunking.

Line mapping: every line inside a hunk gets its line number in the old file
(for context and "-" lines) and in the new file (for context and "+" lines),
counted from the @@ header. Findings use new-file numbers (the prompt asks
for the + side), so new_line_numbers() is what a finding's line is checked
against.

Chunking: the model has a fixed context window (num_ctx), so input is split
or cut to a character budget before it is sent.

- "whole": one request with the entire diff.
- "per_file": one request per changed file.

When a chunk is over budget it is cut at a hunk boundary (or at a line
boundary inside a single oversized hunk), never mid-line, and the cut is
recorded in the plan (truncated, omitted_hunks, omitted_files). Nothing is
dropped silently.
"""

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

WHOLE = "whole"
PER_FILE = "per_file"
STRATEGIES = (WHOLE, PER_FILE)

# Heuristic, not a measurement: source code and diffs average roughly 3-4
# characters per token for BPE tokenizers. 3 is the conservative end, so the
# char budget errs toward fitting inside num_ctx.
CHARS_PER_TOKEN = 3
DEFAULT_OUTPUT_RESERVE_TOKENS = 1024

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # " ", "+", "-"
    text: str
    old_no: Optional[int]
    new_no: Optional[int]


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list = field(default_factory=list)
    raw_lines: list = field(default_factory=list)  # header + body, with line endings

    def file_line(self, index: int) -> tuple[Optional[int], Optional[int]]:
        """(old_no, new_no) for the index-th body line of this hunk."""
        line = self.lines[index]
        return line.old_no, line.new_no

    @property
    def raw(self) -> str:
        return "".join(self.raw_lines)


@dataclass
class FileDiff:
    old_path: Optional[str]
    new_path: Optional[str]
    header_lines: list = field(default_factory=list)
    hunks: list = field(default_factory=list)

    @property
    def path(self) -> Optional[str]:
        return self.new_path if self.new_path is not None else self.old_path

    @property
    def header(self) -> str:
        return "".join(self.header_lines)

    @property
    def raw(self) -> str:
        return self.header + "".join(h.raw for h in self.hunks)

    def new_line_numbers(self) -> set:
        """New-file line numbers visible in the diff (context and added)."""
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.new_no is not None}

    def added_line_numbers(self) -> set:
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.kind == "+"}

    def diff_line_to_new_line(self, hunk_index: int, line_index: int) -> Optional[int]:
        return self.hunks[hunk_index].file_line(line_index)[1]


def strip_ab_prefix(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    path = path.strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _header_path(line: str) -> Optional[str]:
    # "--- a/path\t2026-01-01 ..." : drop an optional tab-separated timestamp.
    value = line[4:].rstrip("\r\n").split("\t", 1)[0]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return strip_ab_prefix(value)


def is_unified_diff(text: str) -> bool:
    return isinstance(text, str) and any(_HUNK_HEADER.match(line) for line in text.splitlines())


def parse_unified_diff(text: str) -> list:
    """Parse a (possibly multi-file) unified diff. Unparseable text gives []."""
    if not isinstance(text, str):
        return []
    files: list = []
    current: Optional[FileDiff] = None
    hunk: Optional[Hunk] = None
    old_left = new_left = 0
    old_no = new_no = 0

    def start_file() -> FileDiff:
        fd = FileDiff(old_path=None, new_path=None)
        files.append(fd)
        return fd

    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")

        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith("\\"):  # "\ No newline at end of file"
                hunk.raw_lines.append(raw)
                continue
            kind = line[:1] if line[:1] in ("+", "-", " ") else (" " if line == "" else None)
            if kind is not None:
                body = line[1:]
                if kind == " ":
                    hunk.lines.append(DiffLine(" ", body, old_no, new_no))
                    old_no += 1
                    new_no += 1
                    old_left -= 1
                    new_left -= 1
                elif kind == "-":
                    hunk.lines.append(DiffLine("-", body, old_no, None))
                    old_no += 1
                    old_left -= 1
                else:
                    hunk.lines.append(DiffLine("+", body, None, new_no))
                    new_no += 1
                    new_left -= 1
                hunk.raw_lines.append(raw)
                continue
            hunk = None  # malformed hunk: counts did not add up; fall through

        if line.startswith("\\") and hunk is not None:
            hunk.raw_lines.append(raw)
            continue

        header = _HUNK_HEADER.match(line)
        if header:
            if current is None:
                current = start_file()
            old_start, old_count = int(header.group(1)), int(header.group(2) or 1)
            new_start, new_count = int(header.group(3)), int(header.group(4) or 1)
            hunk = Hunk(old_start, old_count, new_start, new_count, line, raw_lines=[raw])
            current.hunks.append(hunk)
            old_left, new_left = old_count, new_count
            old_no, new_no = old_start, new_start
            continue

        hunk = None
        if line.startswith("diff --git ") or (
            line.startswith("--- ") and (current is None or current.hunks)
        ):
            current = start_file()
            if line.startswith("diff --git "):
                parts = line[len("diff --git "):].split(" ")
                if len(parts) == 2:
                    current.old_path, current.new_path = strip_ab_prefix(parts[0]), strip_ab_prefix(parts[1])
        if current is None:
            continue  # preamble before the first file (e.g. a commit message)
        current.header_lines.append(raw)
        if line.startswith("--- "):
            current.old_path = _header_path(line)
        elif line.startswith("+++ "):
            current.new_path = _header_path(line)
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to "):].strip()
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from "):].strip()
        elif line.startswith("deleted file"):
            current.new_path = None

    return [fd for fd in files if fd.hunks or fd.header_lines]


def new_line_index(files: list) -> dict:
    """{path: set of new-file line numbers visible in the diff}."""
    return {fd.path: fd.new_line_numbers() for fd in files if fd.path}


def finding_in_diff(finding, files: list) -> bool:
    """
    True if the finding's file is in the diff and its line range touches a
    new-side line shown in the diff. Diagnostic only; this is not the eval's
    catch rule (that is eval/match.py, author-owned).
    """
    if finding.file is None or finding.line_start is None:
        return False
    path = strip_ab_prefix(finding.file.replace("\\", "/"))
    lines = new_line_index(files).get(path)
    if not lines:
        return False
    low, high = sorted((finding.line_start, finding.line_end or finding.line_start))
    return any(low <= n <= high for n in lines)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    path: Optional[str]
    text: str
    original_chars: int
    truncated: bool = False
    omitted_hunks: int = 0


@dataclass
class ChunkPlan:
    strategy: str
    budget_chars: int
    chunks: list = field(default_factory=list)
    omitted_files: list = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return bool(self.omitted_files) or any(c.truncated for c in self.chunks)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "budget_chars": self.budget_chars,
            "truncated": self.truncated,
            "omitted_files": list(self.omitted_files),
            "chunks": [
                {k: v for k, v in asdict(c).items() if k != "text"} | {"chars": len(c.text)}
                for c in self.chunks
            ],
        }


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def char_budget(num_ctx: int, template_chars: int, output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS) -> int:
    """Characters of code that fit in num_ctx next to the prompt template."""
    tokens_for_code = num_ctx - output_reserve_tokens - math.ceil(template_chars / CHARS_PER_TOKEN)
    return max(0, tokens_for_code * CHARS_PER_TOKEN)


def _cut_lines(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    # Last newline at index <= budget - 1, so the kept text is <= budget chars.
    cut = text.rfind("\n", 0, budget)
    return text[: cut + 1] if cut >= 0 else ""


def _fit_file(fd: FileDiff, budget: int) -> tuple[str, int, bool]:
    """(text, omitted_hunks, truncated) for one file within budget."""
    out = fd.header
    if len(out) > budget:
        return "", len(fd.hunks), True
    for index, hunk in enumerate(fd.hunks):
        if len(out) + len(hunk.raw) <= budget:
            out += hunk.raw
            continue
        # First hunk that does not fit: keep as many whole lines as fit, then
        # stop. Later hunks are omitted.
        partial = _cut_lines(hunk.raw, budget - len(out))
        omitted = len(fd.hunks) - index
        if partial.strip() and _HUNK_HEADER.match(partial.splitlines()[0]) and len(partial.splitlines()) > 1:
            out += partial
            omitted -= 1
        return out, omitted, True
    return out, 0, False


def plan_chunks(text: str, strategy: str = WHOLE, budget_chars: int = 24000) -> ChunkPlan:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown chunking strategy {strategy!r}; expected one of {STRATEGIES}")
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")
    text = text or ""
    plan = ChunkPlan(strategy=strategy, budget_chars=budget_chars)
    files = parse_unified_diff(text) if is_unified_diff(text) else []

    if not files:
        # Not a diff (manual snippet): one chunk, cut at a line boundary.
        body = _cut_lines(text, budget_chars)
        plan.chunks.append(Chunk(None, body, len(text), truncated=len(body) < len(text)))
        return plan

    if strategy == PER_FILE:
        for fd in files:
            body, omitted, truncated = _fit_file(fd, budget_chars)
            plan.chunks.append(Chunk(fd.path, body, len(fd.raw), truncated, omitted))
        return plan

    if len(text) <= budget_chars:
        plan.chunks.append(Chunk(None, text, len(text)))
        return plan

    # Whole diff over budget: fill file by file, hunk by hunk.
    # Text outside file sections (e.g. a commit message preamble) is dropped.
    out, omitted_hunks, truncated = "", 0, False
    for index, fd in enumerate(files):
        remaining = budget_chars - len(out)
        body, omitted, cut = _fit_file(fd, remaining)
        if not body:
            truncated = True
            plan.omitted_files.extend(f.path for f in files[index:] if f.path)
            omitted_hunks += sum(len(f.hunks) for f in files[index:])
            break
        out += body
        omitted_hunks += omitted
        if cut:
            truncated = True
            plan.omitted_files.extend(f.path for f in files[index + 1:] if f.path)
            omitted_hunks += sum(len(f.hunks) for f in files[index + 1:])
            break
    plan.chunks.append(Chunk(None, out, len(text), truncated=truncated, omitted_hunks=omitted_hunks))
    return plan
