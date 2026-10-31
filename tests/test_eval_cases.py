import subprocess

import pytest
import yaml

from eval.cases import CaseFileError, git_blob_hash, load_cases

SHA_A = "a" * 40
SHA_B = "b" * 40


def bug(**overrides):
    case = {
        "id": "fake-bug-1",
        "repo_url": "https://github.com/example-org/example-repo",
        "parent_sha": SHA_A,
        "fix_sha": SHA_B,
        "split": "bug",
        "buggy_file": "pkg/mod.py",
        "line_start": 40,
        "line_end": 42,
        "category": "logic",
    }
    case.update(overrides)
    return {k: v for k, v in case.items() if v is not _DROP}


def control(**overrides):
    case = {
        "id": "fake-control-1",
        "repo_url": "https://github.com/example-org/example-repo",
        "parent_sha": SHA_A,
        "fix_sha": SHA_B,
        "split": "control",
    }
    case.update(overrides)
    return case


_DROP = object()


def write(tmp_path, cases, **top):
    data = {"version": 1, "cases": cases, **top}
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def problems(tmp_path, cases, **top):
    with pytest.raises(CaseFileError) as info:
        load_cases(write(tmp_path, cases, **top))
    return info.value.problems


def test_valid_file_loads(tmp_path):
    cs = load_cases(write(tmp_path, [bug(), control()]))
    assert [c.id for c in cs.bugs] == ["fake-bug-1"]
    assert [c.id for c in cs.controls] == ["fake-control-1"]
    label = cs.bugs[0].label
    assert (label.buggy_file, label.line_start, label.line_end, label.category) == ("pkg/mod.py", 40, 42, "logic")
    assert cs.controls[0].label is None
    assert len(cs.blob_hash) == 40


def test_blob_hash_matches_git(tmp_path):
    path = write(tmp_path, [bug()])
    expected = subprocess.run(
        ["git", "hash-object", "--no-filters", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert git_blob_hash(path.read_bytes()) == expected


def test_missing_file_message_points_to_example(tmp_path):
    with pytest.raises(CaseFileError) as info:
        load_cases(tmp_path / "nope.yaml")
    assert "cases.example.yaml" in str(info.value)


def test_invalid_yaml_reports_line(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("version: 1\ncases:\n  - id: [unclosed\n", encoding="utf-8")
    with pytest.raises(CaseFileError) as info:
        load_cases(path)
    assert "invalid YAML at line" in str(info.value)


@pytest.mark.parametrize(
    "case,expected",
    [
        (bug(id=_DROP), "missing required field 'id'"),
        (bug(id="Has Spaces"), "id must match"),
        (bug(repo_url="https://gitlab.com/x/y"), "repo_url must look like"),
        (bug(parent_sha="abc123"), "parent_sha must be a full 40-character"),
        (bug(fix_sha=SHA_B.upper()), "fix_sha must be a full 40-character"),
        (bug(fix_sha=SHA_A), "same commit"),
        (bug(split="maybe"), "split must be one of"),
        (bug(buggy_file=_DROP), "bug case is missing 'buggy_file'"),
        (bug(buggy_file="/abs/mod.py"), "must be relative"),
        (bug(buggy_file="pkg/../mod.py"), "must not contain '..'"),
        (bug(buggy_file="b/pkg/mod.py"), "a/ or b/ prefix"),
        (bug(buggy_file="pkg/mod.js"), "must be a .py file"),
        (bug(line_start=0), "line_start must be an integer >= 1"),
        (bug(line_end="42"), "line_end must be an integer >= 1"),
        (bug(line_start=True), "line_start must be an integer >= 1"),
        (bug(line_start=50, line_end=40), "line_end (40) is before line_start (50)"),
        (bug(line_start=[1, 5]), "multiple ranges are not supported"),
        (bug(ranges=[[1, 2]]), "unknown field 'ranges' (multiple ranges"),
        (bug(severity="CRITICAL"), "unknown field 'severity'"),
        (control(buggy_file="x.py"), "control case must not have 'buggy_file'"),
        (control(category="logic"), "control case must not have 'category'"),
        ("just a string", "must be a mapping"),
    ],
)
def test_case_errors(tmp_path, case, expected):
    found = problems(tmp_path, [case])
    assert any(expected in p for p in found), found


def test_errors_name_the_case(tmp_path):
    found = problems(tmp_path, [bug(), bug(id="fake-bug-2", line_start=0)])
    assert found == ["cases[1] (id=fake-bug-2): line_start must be an integer >= 1, got 0"]


def test_duplicate_ids(tmp_path):
    found = problems(tmp_path, [bug(), bug()])
    assert any("duplicate id (first used at cases[0])" in p for p in found)


def test_all_problems_reported_together(tmp_path):
    found = problems(tmp_path, [bug(line_start=0), control(category="x"), bug(id="b2", split="nope")])
    assert len(found) == 3


def test_categories_list_is_enforced_when_present(tmp_path):
    found = problems(tmp_path, [bug(category="typo")], categories=["logic"])
    assert any("not in the file's categories list" in p for p in found)
    assert load_cases(write(tmp_path, [bug()], categories=["logic"])).categories == ("logic",)


@pytest.mark.parametrize(
    "top,expected",
    [
        ({"version": 2}, "version must be 1"),
        ({"extra": True}, "unknown top-level field 'extra'"),
        ({"categories": "logic"}, "categories must be a list"),
    ],
)
def test_top_level_errors(tmp_path, top, expected):
    found = problems(tmp_path, [bug()], **top)
    assert any(expected in p for p in found), found


def test_empty_cases_rejected(tmp_path):
    assert any("non-empty list" in p for p in problems(tmp_path, []))


def test_top_level_must_be_mapping(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(CaseFileError):
        load_cases(path)
