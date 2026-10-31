"""Guards against config and dependency drift."""

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SOURCES = [
    "main.py", "database.py", "security.py", "retry_queue.py", "simulate_pr.py", "frontend_static.py",
]
REVIEWER_DIR = ROOT / "reviewer"


def _app_files():
    files = [ROOT / name for name in APP_SOURCES]
    if REVIEWER_DIR.is_dir():
        files += sorted(REVIEWER_DIR.glob("*.py"))
    return files


def _env_names_read(path: Path) -> set[str]:
    """Env var names passed as string literals to os.getenv / helper wrappers."""
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            # rate_limit(bucket, ENV_NAME, default): the env name is arg 2.
            position = 1 if name == "rate_limit" else 0
            if name in {"getenv", "_env_int", "_env_float", "_env_str", "rate_limit"}:
                if len(node.args) > position:
                    arg = node.args[position]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
    return names


def test_env_scanner_finds_known_names():
    found = _env_names_read(ROOT / "main.py") | _env_names_read(ROOT / "security.py")
    assert {"WEBHOOK_SECRET", "OLLAMA_MODEL", "RATE_LIMIT_EXECUTE_PER_MINUTE"} <= found


def _env_example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.M))


def test_every_env_var_read_is_documented_in_env_example():
    read = set()
    for path in _app_files():
        read |= _env_names_read(path)
    read.add("CODEREV_API_KEY")  # read through security.API_KEY_ENV
    missing = sorted(read - _env_example_names())
    assert not missing, f".env.example is missing: {missing}"


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


IMPORT_TO_PACKAGE = {"dotenv": "python-dotenv", "sqlalchemy": "sqlalchemy", "yaml": "pyyaml"}


def test_every_third_party_import_is_pinned_in_requirements():
    local = {p.stem for p in ROOT.glob("*.py")} | {"reviewer", "eval"}
    stdlib = set(sys.stdlib_module_names)
    pinned = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)(\[[^\]]*\])?==", line.strip())
        if match:
            pinned.add(match.group(1).lower())

    imported = set()
    for path in _app_files():
        imported |= _top_level_imports(path)
    third_party = {m for m in imported if m not in stdlib and m not in local}
    # starlette ships with fastapi and is only reached through fastapi.* imports.
    missing = sorted(
        m for m in third_party if IMPORT_TO_PACKAGE.get(m, m).lower() not in pinned
    )
    assert not missing, f"requirements.txt is missing: {missing}"


def test_simulate_pr_has_no_hardcoded_secret():
    source = (ROOT / "simulate_pr.py").read_text(encoding="utf-8")
    assert "your_secret_here" not in source
    assert "requests" not in _top_level_imports(ROOT / "simulate_pr.py")


def test_simulate_pr_refuses_without_secret(monkeypatch, capsys):
    import simulate_pr

    monkeypatch.setattr(simulate_pr, "load_dotenv", lambda: None)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(sys, "argv", ["simulate_pr.py"])
    assert simulate_pr.main() == 2


def test_simulate_pr_signs_with_env_secret(monkeypatch):
    import hashlib
    import hmac

    import httpx

    import simulate_pr

    sent = {}

    def fake_post(url, content, headers, timeout):
        sent.update(url=url, content=content, headers=headers)
        return httpx.Response(202, text="{}")

    monkeypatch.setattr(simulate_pr, "load_dotenv", lambda: None)
    monkeypatch.setenv("WEBHOOK_SECRET", "unit-test-secret")
    monkeypatch.setattr(simulate_pr.httpx, "post", fake_post)
    monkeypatch.setattr(sys, "argv", ["simulate_pr.py", "--sha", "abc"])
    assert simulate_pr.main() == 0
    expected = "sha256=" + hmac.new(b"unit-test-secret", sent["content"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Hub-Signature-256"] == expected
