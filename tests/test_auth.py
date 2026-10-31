import pytest

PROTECTED = [
    ("get", "/api/reviews", None),
    ("post", "/api/reviews/manual", {"code_snippet": "print(1)"}),
    ("post", "/api/execute", {"code": "print(1)"}),
]


def _call(client, method, path, body, headers=None):
    kwargs = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_missing_key_is_rejected(client, method, path, body):
    assert _call(client, method, path, body).status_code == 403


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_wrong_key_is_rejected(client, method, path, body):
    assert _call(client, method, path, body, {"X-API-Key": "not-the-key"}).status_code == 403


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_correct_key_is_accepted(client, auth, method, path, body):
    assert _call(client, method, path, body, auth).status_code == 200


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_unset_env_fails_closed(client, auth, monkeypatch, method, path, body):
    monkeypatch.delenv("CODEREV_API_KEY", raising=False)
    response = _call(client, method, path, body, auth)
    assert response.status_code == 403
    assert "not configured" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_empty_env_fails_closed(client, auth, monkeypatch, method, path, body):
    monkeypatch.setenv("CODEREV_API_KEY", "")
    assert _call(client, method, path, body, auth).status_code == 403


def test_health_needs_no_key(client):
    assert client.get("/api/health").status_code == 200


def test_webhook_does_not_use_api_key(client, auth):
    # No signature: rejected by the HMAC check (401), not by the API key (403),
    # even when a valid API key is present.
    response = client.post(
        "/api/webhook/github", content=b"{}", headers={**auth, "X-GitHub-Event": "pull_request"}
    )
    assert response.status_code == 401
