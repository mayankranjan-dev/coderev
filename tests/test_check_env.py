"""scripts/check_env.py reports names only, never values. Uses temporary files, never the real .env."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_env.py"
spec = importlib.util.spec_from_file_location("check_env", SCRIPT)
check_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_env)

SENTINEL = "sentinel-value-should-never-print"


@pytest.fixture
def files(tmp_path):
    example = tmp_path / "example.env"
    example.write_text("# comment\nA=1\nB=\n# C=commented\nexport D=4\n", encoding="utf-8")
    env = tmp_path / "local.env"
    env.write_text(f"A={SENTINEL}\nE={SENTINEL}\nD = {SENTINEL}\nnot a variable line {SENTINEL}\n", encoding="utf-8")
    return env, example


def test_reports_missing_and_extra_names(files, capsys):
    env, example = files
    code = check_env.main(["--env", str(env), "--example", str(example)])
    out = capsys.readouterr().out
    assert code == 1
    missing_block, extra_block = out.split("Extra in")
    assert "  B" in missing_block and "  A" not in missing_block
    assert "  E" in extra_block


def test_values_are_never_printed(files, capsys):
    env, example = files
    check_env.main(["--env", str(env), "--example", str(example)])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


def test_matching_files_exit_zero(tmp_path, capsys):
    a = tmp_path / "a"
    a.write_text("X=1\nY=2\n", encoding="utf-8")
    b = tmp_path / "b"
    b.write_text("Y=secret\nX=other\n", encoding="utf-8")
    assert check_env.main(["--env", str(a), "--example", str(b)]) == 0


def test_unreadable_file_does_not_leak_a_path_or_value(tmp_path, capsys):
    code = check_env.main(["--env", str(tmp_path / "nope"), "--example", str(tmp_path / "nope2")])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_example_file_has_names_the_app_reads():
    names = check_env.variable_names(Path(__file__).resolve().parent.parent / ".env.example")
    assert {"WEBHOOK_SECRET", "CODEREV_API_KEY", "DATABASE_URL"} <= names
