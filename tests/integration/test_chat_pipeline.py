"""
Every decision path through POST /chat, end to end and offline.

Real: the HTTP route, pipeline, signals, rule engine, rule-6 file, catalog,
conversation state (on fakeredis), ticketing client and the PRD v1.1 stand-in
ticketing service (in process).
Stubbed at the edges: embeddings, Qdrant search, the two LLM calls and the
sr_requests writes (an in-memory list).
"""

import json
import logging
from dataclasses import dataclass, field

import fakeredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError, VectorStoreError
from querydesk.main import app
from querydesk.services import conversation, pipeline, rag
from querydesk.services.decision import extraction
from querydesk.services.retrieval import qdrant_store
from querydesk.services.retrieval.qdrant_store import CategoryHit, RetrievedChunk
from querydesk.services.retrieval.query import QueryVectors
from querydesk.services.state import postgres_store, redis_store
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
    # One reply for every extraction, or a list consumed one per call.
    extraction_reply: str | list = '{"application": "claims", "environment": "UAT"}'
    answer_calls: list = field(default_factory=list)
    extraction_calls: list = field(default_factory=list)
    # Queries embedded / searched: a status question must add none.
    embed_calls: list = field(default_factory=list)
    search_calls: int = 0
    # Rows written to sr_requests.
    rows: list = field(default_factory=list)


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
        w.embed_calls.append(query)
        return QueryVectors(dense=[0.1], sparse=object())

    async def fake_hybrid(dense_vector, sparse_vector, top_k=None):
        w.search_calls += 1
        return w.hits, w.dense_score

    async def fake_categories(dense_vector, limit=2):
        return w.categories[:limit]

    async def fake_answer_llm(messages, **kwargs):
        w.answer_calls.append(messages)
        return "No downtime for a clustered service [1]."

    async def fake_extraction_llm(messages, **kwargs):
        w.extraction_calls.append(messages)
        if isinstance(w.extraction_reply, list):
            return w.extraction_reply.pop(0)
        return w.extraction_reply

    async def fake_record_sr(**row):
        w.rows.append({"resolution_type": "AUTO_SR", **row})
        return len(w.rows)

    async def fake_record_escalation(**row):
        w.rows.append({"resolution_type": "MANUAL_ESCALATION", **row})
        return len(w.rows)

    monkeypatch.setattr(pipeline, "embed_query_vectors", fake_embed)
    monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qdrant_store, "search_categories", fake_categories)
    monkeypatch.setattr(rag, "chat_completion", fake_answer_llm)
    monkeypatch.setattr(extraction, "chat_completion", fake_extraction_llm)
    monkeypatch.setattr(postgres_store, "record_sr", fake_record_sr)
    monkeypatch.setattr(postgres_store, "record_escalation", fake_record_escalation)
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    monkeypatch.setattr(redis_store, "_client", fakeredis.aioredis.FakeRedis())

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
        return (await c.get("/sr")).json()["items"]


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
    assert "already have SR 1001 open" in again["reply"] and "PENDING" in again["reply"]
    assert len(await _srs(world)) == 1
    assert len(world.rows) == 1  # only the SR actually raised is recorded


async def _move(world, sr_id, *statuses):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.service), base_url="http://ticketing.test"
    ) as c:
        for status in statuses:
            resp = await c.patch(f"/sr/{sr_id}/status", json={"status": status})
            assert resp.status_code == 200, resp.text


@pytest.mark.anyio
async def test_asking_again_after_the_sr_is_resolved_raises_a_new_sr_the_same_day(world):
    """A closed SR doesn't swallow a repeat: the new key is chained off its SR number."""
    first = (await _chat("restart the claims app in UAT, it's hung")).json()
    await _move(world, first["sr_id"], "IN_PROGRESS", "RESOLVED")

    reopened = (await _chat("restart the claims app in UAT, it's hung")).json()
    resent = (await _chat("restart the claims app in UAT, it's hung")).json()

    assert reopened["sr_id"] != first["sr_id"]
    assert "I've raised SR" in reopened["reply"]
    assert resent["sr_id"] == reopened["sr_id"]  # idempotent again from here
    assert len(await _srs(world)) == 2


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


@pytest.mark.parametrize("search", ["hybrid_search", "search_categories"])
@pytest.mark.anyio
async def test_search_outage_inside_the_task_group_is_a_502(world, monkeypatch, search):
    """Retrieval and routing run in an anyio task group, which wraps errors in a group."""

    async def down(*args, **kwargs):
        raise VectorStoreError("Qdrant unreachable")

    monkeypatch.setattr(qdrant_store, search, down)

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
    [row] = world.rows
    assert row["resolution_type"] == "MANUAL_ESCALATION"
    assert row["details"]["query"] == "what is the capital of Peru?"


@pytest.mark.anyio
async def test_status_question_lists_open_srs_without_a_search(world):
    raised = (await _chat("restart the claims app in UAT, it's hung")).json()
    world.embed_calls.clear()
    world.extraction_calls.clear()
    world.search_calls = 0

    body = (await _chat("what's the status of my requests?")).json()

    assert body["decision"] == "STATUS_LOOKUP"
    assert f"SR {raised['sr_id']}" in body["reply"] and "Pending" in body["reply"]
    assert body["retrieval_confidence"] is None
    # Rule 1 needs only the intent: no embedding, search or LLM call.
    assert world.embed_calls == [] and world.search_calls == 0
    assert world.answer_calls == [] and world.extraction_calls == []


@pytest.mark.anyio
async def test_status_of_someone_elses_sr_is_not_found(world):
    raised = (await _chat("restart the claims app in UAT, it's hung", user_id="u2")).json()

    body = (await _chat(f"any update on SR-{raised['sr_id']}?")).json()

    assert body["decision"] == "STATUS_LOOKUP"
    assert body["reply"] == f"I can't find SR {raised['sr_id']} among your requests."


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
    assert entry["error"] is None
    assert entry["turn"] == 0


@pytest.mark.anyio
async def test_a_failed_sr_creation_still_logs_its_decision(world, monkeypatch, caplog):
    def unreachable():
        raise tk.TicketingError("Ticketing service unreachable")

    monkeypatch.setattr(tk, "_make_client", unreachable)

    with caplog.at_level(logging.INFO, logger="querydesk.decision"):
        resp = await _chat("restart the claims app in UAT, it's hung")

    assert resp.status_code == 502
    [record] = [r for r in caplog.records if r.name == "querydesk.decision"]
    entry = json.loads(record.getMessage())
    assert entry["decision"] == "RAISE_SR"
    assert entry["category_id"] == "it_application_restart"
    assert entry["error"] == "TicketingError"
    assert entry["sr_id"] is None


@pytest.fixture
def decision_rows(world, monkeypatch):
    """decision_logs writes, recorded in memory (POSTGRES_DSN set, so they happen)."""
    rows = []

    async def fake_record_decision(record, *, at):
        rows.append(record)
        return len(rows)

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://fake")
    monkeypatch.setattr(postgres_store, "record_decision", fake_record_decision)
    return rows


@pytest.mark.anyio
async def test_every_decision_path_writes_one_decision_row(world, decision_rows):
    """PRD F10: answer, SR, status, collect + follow-up, cancel, escalate — one row each."""
    replies = [(await _chat("does a rolling restart cause downtime?")).json()]
    replies.append((await _chat("restart the claims app in UAT, it's hung")).json())
    replies.append((await _chat("what's the status of my requests?")).json())
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": null}')
    replies.append((await _chat("extend UAT-2 please", user_id="u2")).json())
    replies.append((await _chat("cancel", user_id="u2")).json())
    replies.append((await _chat("cancel", user_id="u2")).json())  # nothing open
    world.dense_score = 0.17
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.15)]
    replies.append((await _chat("what is the capital of Peru?", user_id="u3")).json())
    await pipeline.drain_decision_writes()

    assert [r["decision"] for r in decision_rows] == [r["decision"] for r in replies]
    assert [r["decision"] for r in replies] == [
        "ANSWER",
        "RAISE_SR",
        "STATUS_LOOKUP",
        "COLLECT_DETAILS",
        "CANCELLED",
        "CANCELLED",
        "ESCALATE",
    ]
    raised = decision_rows[1]
    assert raised["user_id"] == "u1" and raised["sr_id"] == "1001" and raised["rule"] == 4
    assert decision_rows[4]["turn"] == 1  # the cancel answered the open conversation


@pytest.mark.anyio
async def test_a_failed_decision_write_still_returns_the_reply(
    world, decision_rows, monkeypatch, caplog
):
    async def postgres_down(record, *, at):
        raise PostgresStoreError("Postgres connect failed")

    monkeypatch.setattr(postgres_store, "record_decision", postgres_down)

    with caplog.at_level(logging.WARNING, logger="querydesk.services.pipeline"):
        resp = await _chat("restart the claims app in UAT, it's hung")
        await pipeline.drain_decision_writes()

    assert resp.status_code == 200 and resp.json()["sr_id"] == "1001"
    assert "decision_logs write failed" in caplog.text
    assert not pipeline._decision_writes


@pytest.mark.anyio
async def test_no_postgres_dsn_writes_no_decision_row(world, monkeypatch):
    calls = []

    async def record_decision(record, *, at):
        calls.append(record)

    monkeypatch.setattr(postgres_store, "record_decision", record_decision)

    resp = await _chat("does a rolling restart cause downtime?")  # conftest: no POSTGRES_DSN
    await pipeline.drain_decision_writes()

    assert resp.status_code == 200 and calls == []


# --- Slot filling (Day 6, PRD F4) ----------------------------------------------

UAT = [
    CategoryHit("it_uat_extension", "UAT extension", "d", 0.64),
    CategoryHit("it_application_restart", "Application restart", "d", 0.45),
]


def _uat(world, *replies):
    world.categories = UAT
    world.extraction_reply = list(replies)


@pytest.mark.anyio
async def test_uat_extension_raised_from_one_message_plus_one_follow_up(world):
    """Day 6 acceptance: asked only for what's missing, then given the SR number."""
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}',
        '{"environment": "UAT-2", "extend_until": "11 PM", "reason": "release testing"}',
    )

    first = (await _chat("keep UAT-2 up for release testing")).json()

    assert first["decision"] == "COLLECT_DETAILS"
    assert first["missing_fields"] == ["extend_until"]
    assert first["reply"].endswith("I just need the extend until.")
    assert await _srs(world) == []

    second = (await _chat("till 11 PM")).json()

    assert second["decision"] == "RAISE_SR"
    assert second["sr_id"] == "1001"
    assert second["requires_approval"] is True
    assert "approval" in second["reply"]
    [sr] = await _srs(world)
    assert sr["metadata"] == {
        "environment": "UAT-2",
        "extend_until": "11 PM",
        "reason": "release testing",
    }
    assert sr["description"] == "keep UAT-2 up for release testing\ntill 11 PM"
    # Re-extraction saw the whole exchange, so "till 11 PM" kept its context.
    assert "keep UAT-2 up" in world.extraction_calls[1][0]["content"]
    [row] = world.rows
    assert row["resolution_type"] == "AUTO_SR" and row["ticket_id"] == "1001"
    assert await conversation.load("u1") is None


@pytest.mark.anyio
async def test_values_not_stated_in_the_reply_never_overwrite_collected_ones(world):
    """The extractor echoing other values (UAT-9 isn't in "until Friday") changes nothing."""
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": null}',
        '{"environment": "UAT-9", "extend_until": "Friday", "reason": null}',
    )

    await _chat("extend UAT-2 please")
    body = (await _chat("until Friday")).json()

    assert body["decision"] == "COLLECT_DETAILS"
    assert body["missing_fields"] == ["reason"]  # only what's still missing
    assert body["reply"].endswith("I just need the reason.")
    conv = await conversation.load("u1")
    assert conv.fields == {"environment": "UAT-2", "extend_until": "Friday"}
    assert conv.follow_ups == 1


@pytest.mark.anyio
async def test_still_missing_after_two_follow_ups_escalates_and_is_recorded(world):
    _uat(
        world,
        '{"environment": null, "extend_until": null, "reason": null}',
        '{"environment": "UAT-2", "extend_until": null, "reason": null}',
        '{"environment": "UAT-2", "extend_until": "Friday", "reason": null}',
    )

    await _chat("extend my UAT please")
    await _chat("UAT-2")
    body = (await _chat("until Friday")).json()

    assert body["decision"] == "ESCALATE"
    assert body["resolution_type"] == "MANUAL_ESCALATION"
    assert body["reply"].startswith("I still don't have the reason after a couple of tries")
    assert "flagged" in body["reply"]
    assert await _srs(world) == []
    [row] = world.rows
    assert row["resolution_type"] == "MANUAL_ESCALATION"
    assert row["category_id"] == "it_uat_extension"
    assert row["details"]["fields"] == {"environment": "UAT-2", "extend_until": "Friday"}
    assert row["details"]["query"] == "extend my UAT please\nUAT-2\nuntil Friday"
    assert await conversation.load("u1") is None


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", ["cancel", "never mind", "Stop.", "forget it please"])
async def test_cancel_clears_the_conversation(world, cancel):
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": null}')

    await _chat("extend UAT-2 please")
    body = (await _chat(cancel)).json()

    assert body["decision"] == "CANCELLED"
    assert body["reply"] == pipeline.CANCELLED_REPLY
    assert await conversation.load("u1") is None
    assert await _srs(world) == [] and world.rows == []
    assert len(world.extraction_calls) == 1  # the cancel itself costs no LLM call


@pytest.mark.anyio
async def test_a_new_question_mid_conversation_is_answered_and_clears_it(world):
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": null}',
        '{"environment": null, "extend_until": null, "reason": null}',
    )
    await _chat("extend UAT-2 please")

    world.dense_score = 0.77
    body = (await _chat("does a rolling restart cause downtime?")).json()

    assert body["decision"] == "ANSWER"
    assert body["sources"]
    assert await conversation.load("u1") is None
    assert await _srs(world) == []


@pytest.mark.anyio
async def test_prod_given_in_the_follow_up_still_needs_approval(world):
    """Rule 6 applies to the merged fields, not just the first message."""
    world.extraction_reply = [
        '{"application": "claims", "environment": null}',
        '{"application": "claims", "environment": "prod"}',
    ]

    first = (await _chat("restart the claims app please")).json()
    second = (await _chat("prod")).json()

    assert first["decision"] == "COLLECT_DETAILS"
    assert first["requires_approval"] is False
    assert second["decision"] == "RAISE_SR"
    assert second["requires_approval"] is True
    [sr] = await _srs(world)
    assert sr["requires_approval"] is True


@pytest.mark.anyio
async def test_follow_up_logs_its_turn(world, caplog):
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}',
        '{"environment": "UAT-2", "extend_until": "11 PM", "reason": "release testing"}',
    )
    with caplog.at_level(logging.INFO, logger="querydesk.decision"):
        await _chat("keep UAT-2 up for release testing")
        await _chat("till 11 PM")

    entries = [json.loads(r.getMessage()) for r in caplog.records if r.name == "querydesk.decision"]
    assert [(e["turn"], e["decision"]) for e in entries] == [
        (0, "COLLECT_DETAILS"),
        (1, "RAISE_SR"),
    ]
    assert entries[1]["router_top_two"] is None  # a follow-up searches nothing
    assert entries[1]["sr_id"] == "1001"


# --- Duplicate check (PRD F5) ----------------------------------------------------


@pytest.mark.anyio
async def test_an_open_sr_in_the_same_category_is_not_raised_twice(world):
    first = (await _chat("restart the claims app in UAT, it's hung")).json()
    # Different fields, so a different idempotency key: only F5 stops it.
    world.extraction_reply = '{"application": "agent-portal", "environment": "UAT"}'
    again = (await _chat("restart agent-portal in UAT")).json()

    assert again["sr_id"] == first["sr_id"]
    assert f"already have SR {first['sr_id']} open" in again["reply"]
    assert len(await _srs(world)) == 1


@pytest.mark.anyio
async def test_an_open_sr_stops_the_details_being_asked_for_at_all(world):
    """Resending the opening message of a raised UAT extension: no second form to fill in."""
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}',
        '{"environment": "UAT-2", "extend_until": "11 PM", "reason": "release testing"}',
        '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}',
    )
    await _chat("keep UAT-2 up for release testing")
    raised = (await _chat("till 11 PM")).json()

    again = (await _chat("keep UAT-2 up for release testing")).json()

    assert again["decision"] == "RAISE_SR"
    assert again["missing_fields"] == []
    assert again["sr_id"] == raised["sr_id"]
    assert f"already have SR {raised['sr_id']} open" in again["reply"]
    assert await conversation.load("u1") is None
    assert len(await _srs(world)) == 1


@pytest.mark.anyio
async def test_a_closed_sr_in_the_same_category_allows_a_new_one(world):
    first = (await _chat("restart the claims app in UAT, it's hung")).json()
    await _move(world, first["sr_id"], "IN_PROGRESS", "RESOLVED")
    world.extraction_reply = '{"application": "agent-portal", "environment": "UAT"}'

    again = (await _chat("restart agent-portal in UAT")).json()

    assert again["sr_id"] != first["sr_id"]
    assert "I've raised SR" in again["reply"]
    assert len(await _srs(world)) == 2


# --- Degraded stores ---------------------------------------------------------------


class _DownRedis:
    async def get(self, *args, **kwargs):
        raise RedisConnectionError("down")

    set = delete = get


@pytest.mark.anyio
async def test_redis_down_means_single_turn_not_a_500(world, monkeypatch):
    monkeypatch.setattr(redis_store, "_client", _DownRedis())
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": null}')

    resp = await _chat("extend UAT-2 please")

    assert resp.status_code == 200
    assert resp.json()["decision"] == "COLLECT_DETAILS"


async def _postgres_down(**row):
    raise PostgresStoreError("Postgres connect failed")


@pytest.mark.anyio
async def test_postgres_down_still_returns_the_sr(world, monkeypatch):
    monkeypatch.setattr(postgres_store, "record_sr", _postgres_down)

    body = (await _chat("restart the claims app in UAT, it's hung")).json()

    assert body["decision"] == "RAISE_SR" and body["sr_id"] == "1001"


@pytest.mark.anyio
async def test_postgres_down_escalation_falls_back_to_contact_the_desk(world, monkeypatch):
    monkeypatch.setattr(postgres_store, "record_escalation", _postgres_down)
    world.dense_score = 0.17
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.15)]

    body = (await _chat("what is the capital of Peru?")).json()

    assert body["decision"] == "ESCALATE"
    assert body["reply"] == pipeline.ESCALATE_FALLBACK_REPLY


# --- Verification findings (Day 6 follow-up) -------------------------------------


@pytest.mark.anyio
async def test_a_reply_can_correct_a_vague_value_and_nothing_is_invented(world):
    """
    The live repro: the extractor copied the opening sentence into `reason` and
    the user's "UAT-3" was ignored. Values now come from the reply alone.
    """
    _uat(
        world,
        '{"environment": "UAT", "extend_until": null, "reason": null}',
        '{"environment": "UAT-3", "extend_until": null, "reason": "I need my UAT extended please"}',
        '{"environment": null, "extend_until": null, "reason": "regression testing"}',
    )

    await _chat("I need my UAT extended please")
    corrected = (await _chat("it's UAT-3")).json()

    assert corrected["decision"] == "COLLECT_DETAILS"
    assert corrected["missing_fields"] == ["extend_until", "reason"]
    conv = await conversation.load("u1")
    assert conv.fields == {"environment": "UAT-3"}

    answered = (await _chat("because of regression testing")).json()

    # Both follow-ups used: the reason was stated, extend_until never was.
    assert answered["decision"] == "ESCALATE"
    assert "extend until" in answered["reply"]
    [row] = world.rows
    assert row["details"]["fields"] == {"environment": "UAT-3", "reason": "regression testing"}


@pytest.mark.anyio
async def test_follow_up_prompt_keeps_the_original_request_as_context_only(world):
    _uat(
        world,
        '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}',
        '{"environment": null, "extend_until": "11 PM", "reason": null}',
    )
    await _chat("keep UAT-2 up for release testing")
    await _chat("till 11 PM")

    prompt = world.extraction_calls[1][0]["content"]
    assert "Their reply: till 11 PM" in prompt
    assert "context only" in prompt


@pytest.mark.anyio
async def test_an_unhelpful_reply_counts_as_a_follow_up_and_asks_again(world):
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}')
    await _chat("keep UAT-2 up for release testing")

    # "not sure" states no field and makes no sense on its own (rule 5).
    world.extraction_reply = ['{"environment": null, "extend_until": null, "reason": null}'] * 2
    world.dense_score = 0.2
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.2)]
    body = (await _chat("not sure")).json()

    assert body["decision"] == "COLLECT_DETAILS"
    assert body["reply"].endswith("I just need the extend until.")
    conv = await conversation.load("u1")
    assert conv.follow_ups == 1 and conv.turns == ["not sure"]
    assert world.rows == []


@pytest.mark.anyio
async def test_unhelpful_replies_up_to_the_cap_escalate_with_the_conversation(world):
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}')
    await _chat("keep UAT-2 up for release testing")
    world.extraction_reply = ['{"environment": null, "extend_until": null, "reason": null}'] * 4
    world.dense_score = 0.2
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.2)]

    await _chat("not sure")
    body = (await _chat("hmm")).json()

    assert body["decision"] == "ESCALATE"
    assert body["reply"].startswith("I still don't have the extend until")
    assert body["requires_approval"] is None  # no SR, nothing to approve
    [row] = world.rows
    assert row["category_id"] == "it_uat_extension"
    assert row["details"]["fields"] == {"environment": "UAT-2", "reason": "release testing"}
    assert row["details"]["missing"] == ["extend_until"]
    assert await conversation.load("u1") is None


@pytest.mark.anyio
async def test_unhelpful_turn_logs_its_turn_and_why(world, caplog):
    _uat(world, '{"environment": "UAT-2", "extend_until": null, "reason": "release testing"}')
    await _chat("keep UAT-2 up for release testing")
    world.extraction_reply = ['{"environment": null, "extend_until": null, "reason": null}'] * 2
    world.dense_score = 0.2
    world.categories = [CategoryHit("it_password_reset", "Password", "d", 0.2)]

    with caplog.at_level(logging.INFO, logger="querydesk.decision"):
        await _chat("not sure")

    entries = [json.loads(r.getMessage()) for r in caplog.records if r.name == "querydesk.decision"]
    [entry] = [e for e in entries if e["query"] == "not sure"]
    assert entry["turn"] == 1
    assert entry["decision"] == "COLLECT_DETAILS"
    assert "unanswered: the reply stated no field" in entry["overrides"]


@pytest.mark.anyio
async def test_a_correction_is_logged(world, caplog):
    _uat(
        world,
        '{"environment": "UAT", "extend_until": null, "reason": null}',
        '{"environment": "UAT-3", "extend_until": null, "reason": null}',
    )
    with caplog.at_level(logging.INFO, logger="querydesk.decision"):
        await _chat("I need my UAT extended please")
        await _chat("it's UAT-3")

    entries = [json.loads(r.getMessage()) for r in caplog.records if r.name == "querydesk.decision"]
    assert entries[1]["overrides"] == ["corrected: environment"]


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", ["cancel", "never mind"])
async def test_a_bare_cancel_with_nothing_open_records_nothing(world, cancel):
    body = (await _chat(cancel)).json()

    assert body["decision"] == "CANCELLED"
    assert body["reply"] == pipeline.NOTHING_TO_CANCEL_REPLY
    assert world.rows == []
    assert world.extraction_calls == [] and world.answer_calls == []
