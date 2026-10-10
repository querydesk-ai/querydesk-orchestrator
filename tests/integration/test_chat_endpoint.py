"""
POST /chat HTTP contract. rag.answer is patched, so no network and no API key.
"""
import httpx
import pytest

from querydesk.api.routes import chat as chat_router
from querydesk.main import app
from querydesk.models.schemas import ResolutionType, Source
from querydesk.services.llm.fireworks import FireworksError
from querydesk.services.retrieval.qdrant_store import QdrantStoreError


@pytest.fixture
def client():
    # ASGITransport drives the app in-process; no server, no lifespan, so the
    # startup ensure_collections() never reaches for a real Qdrant.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.anyio
async def test_health_is_liveness_only(client):
    async with client as c:
        resp = await c.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_chat_returns_reply_sources_and_confidence(monkeypatch, client):
    async def fake_answer(query):
        return chat_router.rag.RagResult(
            reply="Commission is credited on the 7th [1].",
            resolution_type=ResolutionType.RAG,
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
        )

    monkeypatch.setattr(chat_router.rag, "answer", fake_answer)

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "commission?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution_type"] == "RAG"
    assert body["retrieval_confidence"] == 0.82
    assert len(body["sources"]) == 1
    assert body["sources"][0]["title"] == "Commission schedule"


@pytest.mark.anyio
async def test_escalation_is_a_200_with_no_sources(monkeypatch, client):
    """Escalation is a valid outcome, not an error — the client must get 200."""
    async def fake_answer(query):
        return chat_router.rag.RagResult(
            reply="I don't have information on that.",
            resolution_type=ResolutionType.MANUAL_ESCALATION,
            sources=[],
            retrieval_confidence=0.1,
        )

    monkeypatch.setattr(chat_router.rag, "answer", fake_answer)

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "unrelated"})

    assert resp.status_code == 200
    assert resp.json()["resolution_type"] == "MANUAL_ESCALATION"
    assert resp.json()["sources"] == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [FireworksError("key missing"), QdrantStoreError("qdrant down")],
)
async def test_upstream_failures_map_to_502(monkeypatch, client, error):
    async def fake_answer(query):
        raise error

    monkeypatch.setattr(chat_router.rag, "answer", fake_answer)

    async with client as c:
        resp = await c.post("/chat", json={"user_id": "u1", "query": "anything"})

    # Dependency failure, not bad input.
    assert resp.status_code == 502


@pytest.mark.anyio
async def test_missing_required_field_is_a_422(client):
    async with client as c:
        resp = await c.post("/chat", json={"query": "no user id"})

    assert resp.status_code == 422
