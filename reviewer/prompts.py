"""
Versioned review prompts.

Templates live in prompts/<version>.txt at the repo root and are selected by
REVIEW_PROMPT_VERSION. Each template contains the placeholder {{CODE}}
exactly once; the code is inserted with str.replace (not str.format) because
code is full of braces.

- v1: the original production prompt, cleaned up. Same checklist and rules,
  plus the output contract (path:line location, NO_ISSUES_FOUND sentinel),
  the placeholder-only example, and one severity rule for secrets.
- v2: restructured for a small model. Says how to read +/- lines, gives a
  numbered checklist mapped to severities, and spells out how to count
  new-file line numbers from the @@ header. Whether v2 reviews better than
  v1 is not measured yet; the eval harness (eval/) exists to measure it.

Rules every template must follow (enforced by tests/test_prompt.py):

1. No concrete few-shot examples. The format example uses placeholders only
   (<bug or security issue>, <specific fix>), never real-looking code,
   identifiers, or vulnerability names. A concrete example makes
   qwen2.5-coder:1.5b anchor on it and repeat a lookalike finding even when
   the code does not have that problem (observed during development; not
   measured). Eval-only variants that break this rule must not live in
   prompts/ and must never be loadable from the production path.
2. "Correction: ..." stays on the same line as the severity tag. The parser
   (reviewer/findings.py) splits on newlines, so a correction on its own line
   would not be attached to any finding.
3. A hardcoded secret or credential is CRITICAL, in both the checklist and the
   severity rules.
4. An explicit issue checklist and "report EVERY issue" are kept: without
   them this model tends to report only the first issue it notices.
"""

import re
from functools import lru_cache
from pathlib import Path

CODE_PLACEHOLDER = "{{CODE}}"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_VERSION = "v1"
_VERSION_RE = re.compile(r"^v[0-9]{1,3}$")


class PromptError(ValueError):
    pass


def available_versions() -> list:
    return sorted(p.stem for p in PROMPTS_DIR.glob("v*.txt") if _VERSION_RE.match(p.stem))


@lru_cache(maxsize=None)
def load_template(version: str) -> str:
    # The version is config input: restrict it to vN so it can never name a
    # file outside prompts/.
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PromptError(f"invalid prompt version {version!r}; expected one of {available_versions()}")
    path = PROMPTS_DIR / f"{version}.txt"
    if not path.is_file():
        raise PromptError(f"unknown prompt version {version!r}; expected one of {available_versions()}")
    template = path.read_text(encoding="utf-8")
    if template.count(CODE_PLACEHOLDER) != 1:
        raise PromptError(f"prompts/{version}.txt must contain {CODE_PLACEHOLDER} exactly once")
    return template


def build_prompt(version: str, code_text: str) -> str:
    template = load_template(version)
    before, after = template.split(CODE_PLACEHOLDER)
    # Concatenate rather than replace so a {{CODE}} inside the code is inert,
    # and drop the template's trailing newline so the code ends the prompt.
    return before + code_text + after.rstrip("\n")


def build_review_prompt(code_text: str, version: str = DEFAULT_VERSION) -> str:
    return build_prompt(version, code_text)
