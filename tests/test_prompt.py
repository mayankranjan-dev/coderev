import re

from reviewer.prompts import build_review_prompt


def test_prompt_example_uses_placeholders_only():
    prompt = build_review_prompt("CODE_MARKER")
    example = prompt.split("Example Output", 1)[1].split("Analyze this code", 1)[0]
    assert "<bug or security issue>" in example and "<specific fix>" in example
    example_lines = [line for line in example.splitlines() if re.match(r"^(CRITICAL|WARNING|NITPICK):", line)]
    assert example_lines
    for line in example_lines:
        # Everything after the tag is placeholders, spaces, or contract punctuation.
        body = line.split(":", 1)[1]
        assert re.fullmatch(r"(\s|<[^<>]+>|[:\-]|Correction)+", body), line


def test_prompt_has_no_concrete_code_from_the_old_examples():
    prompt = build_review_prompt("")
    for banned in ("cursor.execute", "os.getenv", "user_count", "API_KEY", "SQL Injection"):
        assert banned not in prompt


def test_prompt_requires_correction_on_same_line_and_sentinel():
    prompt = build_review_prompt("")
    assert "Correction:" in prompt and "same" in prompt and "line" in prompt
    assert "NO_ISSUES_FOUND" in prompt


def test_code_with_braces_is_inserted_verbatim():
    code = "def f():\n    return {'a': {1}}  # {{CODE}}?"
    assert build_review_prompt(code).endswith(code)
