"""
Structured review findings and a tolerant parser for model output.

Output contract (see reviewer/prompts.py): one finding per line,

    <SEVERITY>: <path>:<start>[-<end>] <message> Correction: <fix>

with "Correction: ..." on the same line as the severity tag, plus a sentinel
line NO_ISSUES_FOUND for a clean review. The parser splits on newlines, so a
correction placed on its own line would not belong to any finding.

Small models drift from the contract, so the parser accepts common variants:
markdown emphasis (**CRITICAL:**, **CRITICAL**:), list markers ("1. ",
"- ", "* "), bracketed tags ([CRITICAL]), dash separators, mixed case, and
several location spellings (path:L1-L2, path line 4, `path:3`). A finding
whose location cannot be read is kept with file/line set to None (an
"unlocated" finding). Lines that are not findings are recorded as
ParseFailure data. parse_review() never raises.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

SEVERITIES = ("CRITICAL", "WARNING", "NITPICK")
NO_ISSUES_SENTINEL = "NO_ISSUES_FOUND"
MAX_FAILURE_TEXT = 500


@dataclass
class Finding:
    severity: str
    file: Optional[str]
    line_start: Optional[int]
    line_end: Optional[int]
    message: str
    correction: Optional[str] = None

    # Aliases matching the field names in docs/eval/MATCHING_SPEC.md.
    @property
    def path(self) -> Optional[str]:
        return self.file

    @property
    def text(self) -> str:
        return self.message

    @property
    def located(self) -> bool:
        return self.file is not None and self.line_start is not None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        return cls(
            severity=str(data.get("severity", "")).upper(),
            file=data.get("file"),
            line_start=data.get("line_start"),
            line_end=data.get("line_end"),
            message=data.get("message") or "",
            correction=data.get("correction"),
        )


@dataclass
class ParseFailure:
    line_number: int  # 1-based line in the raw output
    text: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


# ParseResult.status values
OK = "ok"
NO_ISSUES = "no_issues"
EMPTY = "empty"
MALFORMED = "malformed"


@dataclass
class ParseResult:
    findings: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    sentinel_seen: bool = False
    blank: bool = True

    @property
    def status(self) -> str:
        if self.findings:
            return OK
        if self.sentinel_seen:
            return NO_ISSUES
        if self.blank:
            return EMPTY
        return MALFORMED

    def counts(self) -> dict:
        counts = {"critical_count": 0, "warning_count": 0, "nitpick_count": 0}
        for finding in self.findings:
            key = f"{finding.severity.lower()}_count"
            if key in counts:
                counts[key] += 1
        return counts


# Leading list / quote markers: "- ", "* ", "+ ", "• ", "1. ", "2) ", "> ".
_MARKERS = re.compile(r"^(?:\s*(?:[-*+\u2022]\s+|\d{1,3}[.)]\s+|>\s*))*")

# Severity tag with optional emphasis/brackets. The tag must be followed by a
# separator (colon or dash) or be closed by a bracket, so prose such as
# "Critical issues were found" or a rubric echo "CRITICAL = ..." is not a finding.
_TAG = re.compile(
    r"""^(?P<open>[*_`\[(]*)
        (?P<sev>[A-Za-z]+)
        (?P<close>[*_`\])]*)
        \s*(?P<sep>[:\-\u2013\u2014])?
        [*_`]*\s*
        (?P<rest>.*)$""",
    re.X,
)

# "snippet" is the path the prompt asks for when the input is not a diff.
_PATH = r"(?P<path>(?:[A-Za-z]:)?[\w.\-/\\]*[\w\-]\.[A-Za-z0-9_]+|[\w.\-]+(?:[/\\][\w.\-]+)+|snippet)"
_LINES = r"L?(?P<start>\d+)(?:\s*(?:-|\u2013|\u2014|to|:)\s*L?(?P<end>\d+))?"

# Location right after the tag, e.g. "pkg/mod.py:40-42", "`pkg/mod.py:L40`",
# "(pkg/mod.py:41)", "in pkg/mod.py line 41", "pkg/mod.py, lines 40-42".
_LOCATION = re.compile(
    r"^(?:(?:in|at|file:?)\s+)?[`(\[]*" + _PATH + r"[`)\]]*"
    r"\s*(?::|#|,?\s+lines?\s+|,\s*)\s*" + _LINES +
    r"[`)\]]*\s*[:,\-\u2013\u2014]?\s*",
    re.I,
)
# Path with no line number, e.g. "pkg/mod.py: message". Without a line number
# there is less evidence this is a path and not a dotted name such as
# "subprocess.run:", so it must contain a slash or end in a source extension.
_SOURCE_EXT = r"(?:py|pyi|js|jsx|ts|tsx|java|go|rb|rs|c|h|cc|cpp|hpp|cs|php|kt|swift|sh|sql|html|css|ya?ml|json|toml|cfg|ini|md|txt)"
_PATH_ONLY = re.compile(
    r"^(?:(?:in|at|file:?)\s+)?[`(\[]*"
    r"(?P<path>(?:[\w.\-]+[/\\])+[\w.\-]+|[\w.\-]*[\w\-]\." + _SOURCE_EXT + r")"
    r"[`)\]]*\s*[:,\-\u2013\u2014]\s+",
    re.I,
)

_CORRECTION = re.compile(r"\s*[*_]*\bcorrection\b[*_]*\s*:\s*[*_]*", re.I)
_DECORATION_ONLY = re.compile(r"^[`\-=*_#~\s]*$")
_SENTINEL = re.compile(r"^[*_`\s]*NO[_\s]ISSUES[_\s]FOUND[*_`.\s]*$", re.I)


def _clean(text: str) -> str:
    return text.strip().strip("*_`").strip()


def _parse_line(raw: str) -> tuple[Optional[Finding], Optional[str]]:
    """Returns (finding, None) or (None, failure reason)."""
    line = _MARKERS.sub("", raw).strip()
    match = _TAG.match(line)
    if not match:
        return None, "no severity tag"
    severity = match.group("sev").upper()
    bracketed = any(ch in match.group("close") for ch in "])")
    if severity not in SEVERITIES:
        return None, "no severity tag"
    if not (match.group("sep") or bracketed):
        return None, "severity tag without separator"

    rest = match.group("rest").strip()
    file = line_start = line_end = None
    location = _LOCATION.match(rest)
    if location:
        file = location.group("path")
        line_start = int(location.group("start"))
        line_end = int(location.group("end")) if location.group("end") else line_start
        rest = rest[location.end():]
    else:
        path_only = _PATH_ONLY.match(rest)
        if path_only:
            file = path_only.group("path")
            rest = rest[path_only.end():]

    parts = _CORRECTION.split(rest, maxsplit=1)
    message = _clean(parts[0])
    correction = _clean(parts[1]) if len(parts) > 1 else None
    if not message and not correction:
        return None, "severity tag with no message"
    return Finding(severity, file, line_start, line_end, message, correction or None), None


def parse_review(text) -> ParseResult:
    """Parse raw model output. Never raises; non-string input parses as empty."""
    result = ParseResult()
    if not isinstance(text, str):
        return result
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        result.blank = False
        if _DECORATION_ONLY.match(raw):
            continue  # code fences, rules, stray emphasis
        if _SENTINEL.match(_MARKERS.sub("", raw)):
            result.sentinel_seen = True
            continue
        try:
            finding, reason = _parse_line(raw)
        except Exception as exc:  # noqa: BLE001  (defensive: the parser must not crash)
            finding, reason = None, f"parser error: {type(exc).__name__}"
        if finding is not None:
            result.findings.append(finding)
        else:
            result.failures.append(ParseFailure(number, raw.strip()[:MAX_FAILURE_TEXT], reason))
    return result
