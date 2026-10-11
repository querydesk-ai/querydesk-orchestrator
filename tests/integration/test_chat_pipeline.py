"""
Every decision path through POST /chat, end to end and offline.

Real: the HTTP route, pipeline, signals, rule engine, rule-6 file, catalog,
ticketing client and the PRD v1.1 stand-in ticketing service (in process).
Stubbed at the edges: embeddings, Qdrant search and the two LLM calls.
"""

import json
import logging
from dataclasses import dataclass, field

import httpx
import pytest

from querydesk.core.config import settings
from querydesk.main import app
from querydesk.services import pipeline, rag
from querydesk.services.decision import extraction
from querydesk.services.retrieval import qdrant_store
from querydesk.services.retrieval.qdrant_store import CategoryHit, RetrievedChunk
from querydesk.services.retrieval.query import QueryVectors
from querydesk.services.ticketing import client as tk
from tests.fakes.fake_ticketing import create_app


def _chunk():
    return RetrievedChunk(
        text="A rolling restart takes nodes out one at a time.",
        title="Application restart policy",
        source_path="it-application-restart.md",
        source_url="https://kb.internal/it/application-restart",
        category_id="it_application_restart",
        chunk_index=0,
        score=0.5,
    )


@dataclass
class World:
    """The stubbed edges, configurable per test, recording every outside call."""

    dense_score: float = 0.6
    hits: list = field(default_factory=lambda: [_chunk()])
    categories: list = field(
        default_factory=lambda: [
            CategoryHit("it_application_restart", "Application restart", "d", 0.62),
            CategoryHit("it_add_server_cluster", "Add server", "d", 0.41),
        ]
    )
    extraction_reply: str = '{"application": "claims", "environment": "UAT"}'
    answer_calls: list = field(default_factory=list)
    extraction_calls: list = field(default_factory=list)


@pytest.fixture
def world(monkeypatch):
    w = World()
    for name, value in {
        "confidence_high": 0.56,
        "confidence_mid": 0.49,
        "router_min_score": 0.46,
        "router_tie_break_enabled": False,
        "router_tie_margin": 0.0,
        "decision_engine": "rules",
        "ticketing_base_url": "http://ticketing.test",
    }.items():
        monkeypatch.setattr(settings, name, value)

    async def fake_embed(query):
        return QueryVectors(dense=[0.1], sparse=object())

    async def fake_hybrid(dense_vector, sparse_vector, top_k=None):
        return w.hits, w.dense_score

    async def fake_categories(dense_vector, limit=2):
        return w.categories[:limit]

    async def fake_answer_llm(messages, **kwargs):
        w.answer_calls.append(messages)
        return "No downtime for a clustered service [1]."

    async def fake_extraction_llm(messages, **kwargs):
        w.extraction_calls.append(messages)
        return w.extraction_reply

    monkeypatch.setattr(pipeline, "embed_query_vectors", fake_embed)
    monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qdrant_store, "search_categories", fake_categories)
    monkeypatch.setattr(rag, "chat_completion", fake_answer_llm)
    monkeypatch.setattr(extraction, "chat_completion", fake_extraction_llm)

    service = create_app()
    monkeypatch.setattr(
        tk,
        "_make_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://ticketing.test"
        ),
    )
    w.service = service
    return w


async def _chat(query, user_id="u1"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.post("/chat", json={"user_id": user_id, "query": query})


async def _srs(world):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.service), base_url="http://ticketing.test"
    ) as c:
        return (await c.get("/sr")).json()


# --- The SR path ---------------------------------------------------------------


@pytest.mark.anyio
async def test_action_raises_a_real_sr(world):
    resp = await _chat("restart the claims app in UAT, it's hung")

    body = resp.json()
    assert resp.status_code == 200
    assert body["decision"] == "RAISE_SR"
    assert body["resolution_type"] == "AUTO_SR"
    assert body["category_id"] == "it_application_restart"
    assert body["sr_id"] == "1001"
    assert body["requires_approval"] is False
    assert "SR 1001" in body["reply"]

    [sr] = await _srs(world)
    assert sr["metadata"] == {"application": "claims", "environment": "UAT"}
    assert sr["description"] == "restart the claims app in UAT, it's hung"
    assert world.answer_calls == []  # an action is never answered with an article


@pytest.mark.anyio
async def test_resending_the_same_message_returns_the_same_sr(world):
    """Stand-in Checkpoint 1: idempotency end to end."""
    first = (await _chat("restart the claims app in UAT, it's hung")).json()
    again = (await _chat("restart the claims app in UAT, it's hung")).json()

    assert again["sr_id"] == first["sr_id"]
    assert "already raised" in again["reply"]
    assert len(await _srs(world)) == 1


@pytest.mark.anyio
async def test_another_user_gets_their_own_sr(world):
    first = (await _chat("restart the claims app in UAT", user_id="u1")).json()
    other = (await _chat("restart the claims app in UAT", user_id="u2")).json()

    assert first["sr_id"] != other["sr_id"]


@pytest.mark.anyio
async def test_production_restart_requires_approval(world):
    world.extraction_reply = '{"application": "agent-portal", "environment": "prod"}'

    body = (await _chat("please bounce agent-portal in prod")).json()

    assert body["decision"] == "RAISE_SR"
    assert body["requires_approval"] is True
    assert "approval" in body["reply"]
    [sr] = await _srs(world)
    assert sr["requires_approval"] is True


@pytest.mark.anyio
async def test_missing_fields_are_asked_for_and_no_sr_is_created(world):
    world.extraction_reply = '{"application": null, "environment": null}'

    body = (await _chat("app is frozen pls restart")).json()

    assert body["decision"] == "COLLECT_DETAILS"
    assert body["resolution_type"] == "AUTO_SR"
    assert body["missing_fields"] == ["application", "environment"]
    assert "application and environment" in body["reply"]
    assert body["sr_id"] is None
    assert await _srs(world) == []


@pytest.mark.anyio
async def test_failed_extraction_asks_instead_of_failing(world):
    world.extraction_reply = "sorry, I can't do JSON today"

    resp = await _chat("restart the claims app in UAT")

    assert resp.status_code == 200
    assert resp.json()["decision"] == "COLLECT_DETAILS"
    assert await _srs(world) == []


@pytest.mark.anyio
async def test_ticketing_outage_is_a_502(world, monkeypatch):
    def unreachable():
        raise tk.TicketingError("Ticketing service unreachable")

    monkeypatch.setattr(tk, "_make_client", unreachable)

    resp = await _chat("restart the claims app in UAT")

    assert resp.status_code == 502


# --- The answer path -----------------------------------------------------------


@pytest.mark.anyio
async def test_confident_question_is_answered_with_a_citation(world):
    world.dense_score = 0.77

    body = (await _chat("does a rolling restart cause downtime?")).json()

    assert body["decision"] == "ANSWER"
    assert body["resolution_type"] == "RAG"
    assert body["sources"][0]["source_path"] == "it-application-restart.md"
    assert body["retrieval_confidence"] == 0.77
    assert world.extraction_calls == []  # no SR-path LLM spend on a question
    assert await _srs(world) == []


@pytest.mark.anyio
async def test_mid_band_question_gets_the_disclaimer(world):
    world.dense_score = 0.52

    body = (await _chat("does a rolling restart cause downtime?")).json()

    assert body["decision"] == "ANSWER_WITH_DISCLAIMER"
    assert body["reply"].endswith(pipeline.DISCLAIMER)


# --- Escalation and status -----------------------------------------------------


@pytest.mark.anyio
async def test_out_of_scope_escalates_with_no_llm_call_at_all(world):
    world.dense_score = 0.17
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.15)]

    body = (await _chat("what is the capital of Peru?")).json()

    assert body["decision"] == "ESCALATE"
    assert body["resolution_type"] == "MANUAL_ESCALATION"
    assert body["reply"] == pipeline.ESCALATE_REPLY
    assert world.answer_calls == [] and world.extraction_calls == []


@pytest.mark.anyio
async def test_status_question_is_routed_to_status_lookup(world):
    body = (await _chat("what's the status of my request?")).json()

    assert body["decision"] == "STATUS_LOOKUP"
    assert body["reply"] == pipeline.STATUS_REPLY
    assert world.answer_calls == [] and world.extraction_calls == []


# --- Decision log (PRD F10) ----------------------------------------------------


@pytest.mark.anyio
async def test_every_message_logs_one_structured_decision(world, caplog):
    with caplog.at_level(logging.INFO, logger="querydesk.decision"):
        await _chat("restart the claims app in UAT, it's hung")

    [record] = [r for r in caplog.records if r.name == "querydesk.decision"]
    entry = json.loads(record.getMessage())
    assert entry["decision"] == "RAISE_SR"
    assert entry["rule"] == 4
    assert entry["engine"] == "rules"
    assert entry["sr_id"] == "1001"
    assert entry["router_top_two"][0][0] == "it_application_restart"
    assert entry["intent"] == {"is_action": True, "is_status": False}
    assert set(entry["latency_ms"]) >= {"embed", "search", "decide", "extraction", "total"}
