"""
Shared fixtures.

Every test gets its own temporary SQLite file, and no test may touch the
network: a guard on httpx's async transport fails any real outbound request.
Ollama and Judge0 are replaced with in-process stubs.
"""

import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import-time config must never pick up the developer's real .env values for
# anything that would reach the network or the real database.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# The retry worker is exercised directly through retry_queue.run_once().
os.environ["RETRY_WORKER_ENABLED"] = "0"

import database  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Fail loudly if any code under test opens a real async HTTP request."""

    async def _blocked(self, request):
        raise NetworkBlocked(f"network disabled in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    import security

    security.limiter.reset()
    yield
    security.limiter.reset()


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    database.configure(url)
    database.init_db()
    yield url
    database.engine.dispose()


@pytest.fixture
def db_session(db_url):
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()


class OllamaStub:
    """Records calls and returns a canned review result."""

    def __init__(self):
        self.calls = []
        self.result = {
            "review_text": "",
            "critical_count": 0,
            "warning_count": 0,
            "nitpick_count": 0,
            "status": "reviewed",
        }
        self.reachable = True

    async def review(self, code_text):
        self.calls.append(code_text)
        return dict(self.result)

    async def check(self):
        return self.reachable


class Judge0Stub:
    def __init__(self):
        self.calls = []
        self.result = {"stdout": "", "stderr": ""}
        self.error = None

    async def execute(self, code):
        self.calls.append(code)
        if self.error is not None:
            raise self.error
        return dict(self.result)


@pytest.fixture
def ollama_stub(monkeypatch):
    stub = OllamaStub()
    monkeypatch.setattr(main, "review_code_with_ollama", stub.review)
    monkeypatch.setattr(main, "check_ollama_reachable", stub.check)
    return stub


@pytest.fixture
def judge0_stub(monkeypatch):
    stub = Judge0Stub()
    monkeypatch.setattr(main, "execute_code_with_judge0", stub.execute)
    return stub


class DiffStub:
    def __init__(self):
        self.calls = []
        self.text = "diff --git a/x.py b/x.py\n+x = 1\n"

    async def fetch(self, diff_url):
        self.calls.append(diff_url)
        return self.text


@pytest.fixture
def diff_stub(monkeypatch):
    stub = DiffStub()
    monkeypatch.setattr(main, "fetch_diff_text", stub.fetch)
    return stub


TEST_WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    return TEST_WEBHOOK_SECRET


TEST_API_KEY = "test-key"


@pytest.fixture
def auth():
    return {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def client(db_url, ollama_stub, judge0_stub, diff_stub, webhook_secret, monkeypatch):
    monkeypatch.setenv("CODEREV_API_KEY", TEST_API_KEY)
    with TestClient(main.app) as test_client:
        yield test_client
