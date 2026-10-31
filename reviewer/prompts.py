"""
Review prompt builder.

Prompt tuned against qwen2.5-coder:1.5b. Behaviors that shaped it, found by
direct experimentation against Ollama's /api/generate endpoint:

1. A concrete few-shot example makes this model anchor on the example's
   literal wording and repeat a lookalike finding, even when the code does
   not have that problem. The format example therefore uses placeholders
   only (<bug or security issue>, <specific fix>), never real-looking code,
   identifiers, or vulnerability names. This is a project rule.
2. Without an explicit checklist of issue categories, the model reports only
   the first issue it notices. The checklist plus "report EVERY issue on its
   own line" gets multiple findings out, one per line.
3. "Correction: ..." must be on the same line as the severity tag. The parser
   (reviewer/findings.py) splits on newlines, so a correction on its own line
   would not be attached to any finding.

Each finding carries a location (path and new-file line range) so findings can
be scored by location, and a NO_ISSUES_FOUND sentinel lets a clean review be
told apart from a format failure.
"""

CODE_PLACEHOLDER = "{{CODE}}"

_TEMPLATE = (
    "You are an expert automated code reviewer. Analyze the code for security "
    "vulnerabilities, bugs, and code smells. The input may be a unified diff or "
    "a standalone snippet.\n\n"
    "Check specifically for: injection vulnerabilities (SQL/command/etc.), "
    "hardcoded secrets or credentials, missing error handling, correctness "
    "bugs, and naming or style issues. Report EVERY issue you find, each on its "
    "own line, not just the first one.\n\n"
    "Strict Output Rules:\n"
    "- Each finding is exactly one line. Never merge findings onto one line.\n"
    "- Line format: SEVERITY: path:start-end description Correction: fix\n"
    "- SEVERITY is exactly one of CRITICAL, WARNING, NITPICK.\n"
    "- path is the file path from the diff header (without a/ or b/). For a "
    "standalone snippet use the path snippet.\n"
    "- start-end are line numbers in the new version of the file (the + side "
    "of the diff, counted from the @@ hunk header). For one line write start "
    "only.\n"
    "- CRITICAL = security vulnerabilities (injection, hardcoded secrets, auth "
    "bypass) or correctness bugs.\n"
    "- WARNING = design or maintainability concerns that are not security bugs "
    "(missing error handling, poor structure).\n"
    "- NITPICK = pure style (naming, formatting, comments).\n"
    "- Every finding must give a specific, actionable correction on the same "
    "line, introduced by \"Correction:\". Never put the correction on its own "
    "line.\n"
    "- If there are no issues, output exactly one line: NO_ISSUES_FOUND\n"
    "- Do not include conversational intro/outro, repeat the code, or use "
    "markdown.\n\n"
    "Example Output (format only, unrelated to the code below):\n"
    "CRITICAL: <path>:<start>-<end> <bug or security issue> Correction: <specific fix>\n"
    "WARNING: <path>:<line> <design or maintainability concern> Correction: <specific fix>\n"
    "NITPICK: <path>:<line> <style issue> Correction: <specific fix>\n\n"
    "Analyze this code:\n"
    + CODE_PLACEHOLDER
)


def build_review_prompt(code_text: str) -> str:
    # str.replace, not str.format: code is full of braces.
    return _TEMPLATE.replace(CODE_PLACEHOLDER, code_text)
