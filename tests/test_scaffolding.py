import asyncio

import httpx
import pytest

from conftest import NetworkBlocked


def test_health_uses_stub(client, ollama_stub):
    ollama_stub.reachable = False
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ollama_reachable": False}


def test_each_test_gets_an_empty_database(db_session):
    from models import PRReview

    assert db_session.query(PRReview).count() == 0


def test_real_network_is_blocked():
    async def call():
        async with httpx.AsyncClient() as http:
            await http.get("http://example.invalid/")

    with pytest.raises(NetworkBlocked):
        asyncio.run(call())
