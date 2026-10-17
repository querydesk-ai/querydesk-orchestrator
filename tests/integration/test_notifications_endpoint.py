"""
GET /notifications and POST /notifications/read HTTP contract. The store is
patched, so no database: the routes only validate, delegate and map errors.
"""

from datetime import UTC, datetime

import httpx
import pytest

from querydesk.api.routes import notifications as route
from querydesk.core.errors import PostgresStoreError
from querydesk.main import app

ROW = {
    "id": 7,
    "sr_id": "1001",
    "status": "APPROVED",
    "message": "Your request SR 1001 was approved.",
    "created_at": datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
    "read_at": None,
}


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def store(monkeypatch):
    calls = []

    async def list_notifications(user_id, *, unread_only, limit):
        calls.append(("list", user_id, unread_only, limit))
        return [ROW]

    async def mark_read(user_id, ids):
        calls.append(("read", user_id, ids))
        return len(ids)

    monkeypatch.setattr(route.postgres_store, "list_notifications", list_notifications)
    monkeypatch.setattr(route.postgres_store, "mark_read", mark_read)
    return calls


@pytest.mark.anyio
async def test_list_defaults_to_all_recent(client, store):
    async with client as c:
        resp = await c.get("/notifications", params={"user_id": "u1"})

    assert resp.status_code == 200
    [item] = resp.json()
    assert item["sr_id"] == "1001" and item["read_at"] is None
    assert store == [("list", "u1", False, 20)]


@pytest.mark.anyio
async def test_list_unread_only_with_a_limit(client, store):
    async with client as c:
        await c.get("/notifications", params={"user_id": "u1", "unread_only": True, "limit": 5})

    assert store == [("list", "u1", True, 5)]


@pytest.mark.anyio
@pytest.mark.parametrize("params", [{}, {"user_id": ""}, {"user_id": "u1", "limit": 1000}])
async def test_bad_list_parameters_are_422(client, store, params):
    async with client as c:
        assert (await c.get("/notifications", params=params)).status_code == 422
    assert store == []


@pytest.mark.anyio
async def test_mark_read(client, store):
    async with client as c:
        resp = await c.post("/notifications/read", json={"user_id": "u1", "ids": [7, 8]})

    assert resp.status_code == 200 and resp.json() == {"marked": 2}
    assert store == [("read", "u1", [7, 8])]


@pytest.mark.anyio
async def test_a_store_failure_is_502(client, monkeypatch):
    async def down(*args, **kwargs):
        raise PostgresStoreError("down")

    monkeypatch.setattr(route.postgres_store, "list_notifications", down)
    monkeypatch.setattr(route.postgres_store, "mark_read", down)

    async with client as c:
        assert (await c.get("/notifications", params={"user_id": "u1"})).status_code == 502
        resp = await c.post("/notifications/read", json={"user_id": "u1", "ids": [1]})
        assert resp.status_code == 502
