import re

import pytest
from fastapi.testclient import TestClient

import main
from reviewer import prompts
from reviewer.prompts import PromptError, build_prompt, load_template

VERSIONS = ["v1", "v2"]
TAG_LINE = re.compile(r"^(CRITICAL|WARNING|NITPICK):")


def test_expected_versions_exist():
    assert set(VERSIONS) <= set(prompts.available_versions())


def _example_block(template):
    return template.split("Example Output", 1)[1].split(prompts.CODE_PLACEHOLDER, 1)[0]


@pytest.mark.parametrize("version", VERSIONS)
def test_example_uses_placeholders_only(version):
    example = _example_block(load_template(version))
    lines = [line for line in example.splitlines() if TAG_LINE.match(line)]
    assert len(lines) == 3
    assert "<bug or security issue>" in example and "<specific fix>" in example
    for line in lines:
        body = line.split(":", 1)[1]
        assert re.fullmatch(r"(\s|<[^<>]+>|[:\-]|Correction)+", body), line


@pytest.mark.parametrize("version", VERSIONS)
def test_no_tag_line_outside_the_example_is_concrete(version):
    # Any line in the whole template that looks like a finding must be a
    # placeholder line; this catches a concrete example added elsewhere.
    for line in load_template(version).splitlines():
        if TAG_LINE.match(line):
            assert "<" in line and ">" in line, line


@pytest.mark.parametrize("version", VERSIONS)
def test_no_concrete_code_from_the_old_examples(version):
    template = load_template(version)
    for banned in ("cursor.execute", "os.getenv", "user_count", "API_KEY", "user_id", "SQL Injection"):
        assert banned not in template


@pytest.mark.parametrize("version", VERSIONS)
def test_correction_same_line_and_sentinel(version):
    template = load_template(version)
    assert "Correction:" in template
    assert re.search(r"same line", template)
    assert "Never put the correction on its own line" in template
    assert "NO_ISSUES_FOUND" in template


@pytest.mark.parametrize("version", VERSIONS)
def test_secrets_are_consistently_critical(version):
    template = load_template(version)
    assert re.search(r"secret or credential is always CRITICAL", template)
    warning_rule = next(line for line in template.splitlines() if line.lstrip("- ").startswith("WARNING"))
    assert "secret" not in warning_rule.lower() and "credential" not in warning_rule.lower()


@pytest.mark.parametrize("version", VERSIONS)
def test_checklist_and_every_issue_kept(version):
    template = load_template(version)
    assert "EVERY issue" in template


@pytest.mark.parametrize("version", VERSIONS)
def test_code_is_inserted_verbatim_at_the_end(version):
    code = "def f():\n    return {'a': {1}}  # {{CODE}} {x}"
    prompt = build_prompt(version, code)
    assert prompt.endswith(code)
    assert prompt.count(code) == 1


@pytest.mark.parametrize("bad", ["v9", "../etc/passwd", "v1.txt", "", None, "V1", "v1/../v2"])
def test_invalid_versions_are_rejected(bad):
    with pytest.raises(PromptError):
        load_template(bad)


def test_template_must_have_one_placeholder(tmp_path, monkeypatch):
    (tmp_path / "v7.txt").write_text("no placeholder", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    prompts.load_template.cache_clear()
    try:
        with pytest.raises(PromptError):
            load_template("v7")
    finally:
        prompts.load_template.cache_clear()


def test_production_uses_configured_version(monkeypatch):
    monkeypatch.setattr(main, "REVIEW_PROMPT_VERSION", "v2")
    assert main.build_review_prompt("X") == build_prompt("v2", "X")
    monkeypatch.setattr(main, "REVIEW_PROMPT_VERSION", "v1")
    assert main.build_review_prompt("X") == build_prompt("v1", "X")


def test_startup_fails_on_unknown_version(db_url, monkeypatch):
    monkeypatch.setattr(main, "REVIEW_PROMPT_VERSION", "v99")
    with pytest.raises(PromptError):
        with TestClient(main.app):
            pass


def test_prompts_dir_has_no_eval_variants():
    # Eval-only prompt variants must never be loadable from production.
    names = {p.name for p in prompts.PROMPTS_DIR.iterdir()}
    assert all(re.fullmatch(r"v[0-9]{1,3}\.txt", name) for name in names), names
