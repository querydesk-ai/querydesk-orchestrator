"""
POST /webhooks/sr-status HTTP contract. The Celery publish is patched, so no
broker: the route must verify, validate and enqueue — nothing else.
"""

import json

import httpx
import pytest

from querydesk.api.routes import webhooks
from querydesk.core.config import settings
from querydesk.main import app
from querydesk.services.notifications import signing

EVENT = {
    "sr_id": "1001",
    "user_id": "u1",
    "from_status": "PENDING",
    "to_status": "APPROVED",
    "note": "ok for tonight",
    "changed_at": "2026-10-16T10:00:00+00:00",
}


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def queued(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://broker.test")
    events = []
    monkeypatch.setattr(webhooks, "_enqueue", events.append)
    return events


async def _post(client, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[signing.SIGNATURE_HEADER] = signature
    async with client as c:
        return await c.post("/webhooks/sr-status", content=body, headers=headers)


@pytest.mark.anyio
async def test_a_signed_event_is_queued_and_accepted(client, queued):
    body = json.dumps(EVENT).encode()

    resp = await _post(client, body, signing.sign(body))

    assert resp.status_code == 202
    [event] = queued
    assert event.model_dump() == EVENT


@pytest.mark.anyio
@pytest.mark.parametrize("signature", [None, "deadbeef"])
async def test_a_bad_or_missing_signature_is_401_and_not_queued(client, queued, signature):
    resp = await _post(client, json.dumps(EVENT).encode(), signature)

    assert resp.status_code == 401
    assert queued == []


@pytest.mark.anyio
async def test_the_signature_covers_the_raw_bytes(client, queued):
    # Same JSON, different bytes: a re-serialised body must not verify.
    signed = json.dumps(EVENT).encode()
    sent = json.dumps(EVENT, indent=2).encode()

    assert (await _post(client, sent, signing.sign(signed))).status_code == 401


@pytest.mark.anyio
async def test_a_signed_but_malformed_event_is_422(client, queued):
    body = json.dumps({"sr_id": "1001"}).encode()

    resp = await _post(client, body, signing.sign(body))

    assert resp.status_code == 422
    assert queued == []


@pytest.mark.anyio
async def test_a_broker_failure_is_503_so_the_sender_retries(client, monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://broker.test")

    def down(event):
        raise ConnectionError("broker down")

    monkeypatch.setattr(webhooks, "_enqueue", down)
    body = json.dumps(EVENT).encode()

    assert (await _post(client, body, signing.sign(body))).status_code == 503


@pytest.mark.anyio
async def test_no_broker_configured_is_503(client):
    # conftest leaves REDIS_URL empty.
    body = json.dumps(EVENT).encode()

    assert (await _post(client, body, signing.sign(body))).status_code == 503


@pytest.mark.anyio
async def test_an_integer_sr_id_from_the_contract_is_accepted(client, queued):
    # Contract v1.1 sends sr_id as an integer; it used to be rejected with 422.
    body = json.dumps({**EVENT, "sr_id": 1001}).encode()

    resp = await _post(client, body, signing.sign(body))

    assert resp.status_code == 202
    [event] = queued
    assert event.sr_id == "1001"
