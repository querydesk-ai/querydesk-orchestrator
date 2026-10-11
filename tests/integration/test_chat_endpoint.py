"""
POST /chat HTTP contract. pipeline.handle_message is patched, so no network
and no API key. The pipeline itself is exercised in test_chat_pipeline.py.
"""

import httpx
import pytest

from querydesk.api.routes import chat as chat_router
from querydesk.core.errors import TicketingError
from querydesk.main import app
from querydesk.models.schemas import Decision, ResolutionType, Source
from querydesk.services.llm.fireworks import FireworksError
from querydesk.services.pipeline import ChatResult
from querydesk.services.retrieval.qdrant_store import QdrantStoreError


@pytest.fixture
def client():
    # ASGITransport drives the app in-process; no server, no lifespan, so the
    # startup ensure_collections() never reaches for a real Qdrant.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _patch(monkeypatch, result=None, error=None):
    async def fake_handle(user_id, query):
        if error:
            raise error
        return result

    monkeypatch.setattr(chat_router.pipeline, "handle_message", fake_handle)


@pytest.mark.anyio
async def test_health_is_liveness_only(client):
    async with client as c:
        resp = await c.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_answer_returns_reply_sources_confidence_and_decision(monkeypatch, client):
    _patch(
        monkeypatch,
        ChatResult(
            reply="Commission is credited on the 7th [1].",
            resolution_type=ResolutionType.RAG,
            decision=Decision.ANSWER,
            sources=[
                Source(
                    title="Commission schedule",
                    source_path="commission.md",
                    source_url="https://kb.internal/commission",
                    category_id="ops_commission_payout_query",
                    snippet="Commission is credited on the 7th.",
                    score=0.5,
                )
            ],
            retrieval_confidence=0.82,
        ),
    )

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "commission?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution_type"] == "RAG"
    assert body["decision"] == "ANSWER"
    assert body["retrieval_confidence"] == 0.82
    assert body["sources"][0]["title"] == "Commission schedule"
    assert body["sr_id"] is None


@pytest.mark.anyio
async def test_raised_sr_carries_its_id(monkeypatch, client):
    _patch(
        monkeypatch,
        ChatResult(
            reply="I've raised SR 1001.",
            resolution_type=ResolutionType.AUTO_SR,
            decision=Decision.RAISE_SR,
            category_id="it_application_restart",
            sr_id="1001",
            requires_approval=False,
        ),
    )

    async with client as c:
        body = (await c.post("/chat", json={"user_id": "u1", "query": "restart"})).json()

    assert body["resolution_type"] == "AUTO_SR"
    assert body["sr_id"] == "1001"
    assert body["category_id"] == "it_application_restart"


@pytest.mark.anyio
async def test_escalation_is_a_200_with_no_sources(monkeypatch, client):
    """Escalation is a valid outcome, not an error — the client must get 200."""
    _patch(
        monkeypatch,
        ChatResult(
            reply="A person will pick it up.",
            resolution_type=ResolutionType.MANUAL_ESCALATION,
            decision=Decision.ESCALATE,
            retrieval_confidence=0.1,
        ),
    )

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "unrelated"})

    assert resp.status_code == 200
    assert resp.json()["resolution_type"] == "MANUAL_ESCALATION"
    assert resp.json()["sources"] == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [FireworksError("key missing"), QdrantStoreError("qdrant down"), TicketingError("down")],
)
async def test_upstream_failures_map_to_502(monkeypatch, client, error):
    _patch(monkeypatch, error=error)

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "anything"})

    # Dependency failure, not bad input.
    assert resp.status_code == 502


@pytest.mark.anyio
async def test_missing_required_field_is_a_422(client):
    async with client as c:
        resp = await c.post("/chat", json={"query": "no user id"})

    assert resp.status_code == 422
