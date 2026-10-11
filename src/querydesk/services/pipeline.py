"""
One chat message, end to end:

  embed once -> retrieve + route (concurrently) -> classify -> decide -> act

`act` is the only place a decision turns into side effects: generating an
answer, creating an SR, asking for missing details, or escalating. Every
message logs one structured decision line (PRD F10); it moves to the
decision_logs table with Postgres on Day 6.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field

import anyio

from querydesk.models.schemas import Decision, ResolutionType, Source
from querydesk.services import catalog, rag
from querydesk.services.decision.engine import (
    DecisionEngine,
    DecisionInput,
    DecisionOutput,
    get_engine,
)
from querydesk.services.decision.signals import classify
from querydesk.services.retrieval.query import embed_query_vectors
from querydesk.services.routing.router import RouterResult, route
from querydesk.services.ticketing import client as ticketing

decision_log = logging.getLogger("querydesk.decision")

ESCALATE_REPLY = (
    "I can't handle this one automatically, so I've flagged it for the service desk team. "
    "A person will pick it up."
)
STATUS_REPLY = (
    "I can't look up the status of existing requests yet — that's coming soon. "
    "You'll be notified automatically whenever one of your requests changes status."
)
DISCLAIMER = (
    '\n\nThis may not fully cover your case. If it doesn\'t, say "raise a request" '
    "and I'll open one for you."
)


@dataclass
class ChatResult:
    reply: str
    resolution_type: ResolutionType
    decision: Decision
    sources: list[Source] = field(default_factory=list)
    retrieval_confidence: float | None = None
    category_id: str | None = None
    sr_id: str | None = None
    missing_fields: list[str] = field(default_factory=list)
    requires_approval: bool | None = None


def _readable(field_name: str) -> str:
    return field_name.replace("_", " ")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


async def _act(
    user_id: str, query: str, retrieval: rag.Retrieval, out: DecisionOutput
) -> ChatResult:
    base = dict(
        decision=out.decision,
        retrieval_confidence=retrieval.top_dense_score,
        category_id=out.category_id,
        requires_approval=out.requires_approval,
    )

    if out.decision in (Decision.ANSWER, Decision.ANSWER_WITH_DISCLAIMER):
        answer = await rag.generate(query, retrieval)
        reply = answer.reply
        if out.decision is Decision.ANSWER_WITH_DISCLAIMER:
            reply += DISCLAIMER
        return ChatResult(
            reply=reply, resolution_type=answer.resolution_type, sources=answer.sources, **base
        )

    if out.decision is Decision.RAISE_SR:
        category = catalog.get(out.category_id)
        key = ticketing.idempotency_key(user_id, category.id, out.fields)
        record = await ticketing.create_sr(
            ticketing.SRCreate(
                user_id=user_id,
                category_id=category.id,
                description=query,
                requires_approval=bool(out.requires_approval),
                metadata=out.fields,
            ),
            key,
        )
        if record.replayed:
            reply = f"You already raised this today — it's SR {record.id} ({category.name})."
        else:
            reply = f"I've raised SR {record.id} for {category.name}."
        reply += (
            " It needs approval before work starts; you'll be notified as it moves."
            if out.requires_approval
            else " You'll be notified as it progresses."
        )
        return ChatResult(
            reply=reply, resolution_type=ResolutionType.AUTO_SR, sr_id=record.id, **base
        )

    if out.decision is Decision.COLLECT_DETAILS:
        category = catalog.get(out.category_id)
        wanted = _join([_readable(f) for f in out.missing_fields])
        return ChatResult(
            # No "a/an" before the name: "a UAT…" vs "an Application…" can't be
            # decided from spelling, so the sentence avoids needing one.
            reply=f"I can raise this as a request ({category.name}). I just need the {wanted}.",
            resolution_type=ResolutionType.AUTO_SR,
            missing_fields=out.missing_fields,
            **base,
        )

    if out.decision is Decision.STATUS_LOOKUP:
        return ChatResult(reply=STATUS_REPLY, resolution_type=out.resolution_type, **base)

    # ESCALATE: a fixed reply and no LLM call.
    return ChatResult(reply=ESCALATE_REPLY, resolution_type=out.resolution_type, **base)


def _log(user_id, query, retrieval, router, intent, out, result, timings) -> None:
    decision_log.info(
        json.dumps(
            {
                "user_id": user_id,
                "query": query,
                "top_dense_score": round(retrieval.top_dense_score, 4),
                "router_top_two": [[c.category_id, round(c.score, 4)] for c in router.top_two],
                "router_confident": router.confident,
                "intent": asdict(intent),
                "engine": out.engine,
                "decision": out.decision.value,
                "rule": out.rule,
                "category_id": out.category_id,
                "missing_fields": out.missing_fields,
                "requires_approval": out.requires_approval,
                "overrides": out.overrides,
                "extraction_ok": out.extraction_ok,
                "sr_id": result.sr_id,
                "latency_ms": timings,
            }
        )
    )


async def handle_message(
    user_id: str, query: str, engine: DecisionEngine | None = None
) -> ChatResult:
    start = time.perf_counter()

    vectors = await embed_query_vectors(query)
    embedded = time.perf_counter()

    found: dict = {}

    async def _retrieve():
        found["retrieval"] = await rag.retrieve(query, vectors)

    async def _route():
        found["router"] = await route(query, vectors.dense)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_retrieve)
        tg.start_soon(_route)
    retrieval: rag.Retrieval = found["retrieval"]
    router: RouterResult = found["router"]
    searched = time.perf_counter()

    intent = classify(query)
    out = await (engine or get_engine()).decide(
        DecisionInput(
            query=query,
            top_dense_score=retrieval.top_dense_score,
            has_hits=bool(retrieval.hits),
            router=router,
            intent=intent,
        )
    )
    decided = time.perf_counter()

    result = await _act(user_id, query, retrieval, out)
    done = time.perf_counter()

    timings = {
        "embed": round((embedded - start) * 1000, 1),
        "search": round((searched - embedded) * 1000, 1),
        # The decision layer's own cost; the LLM extraction is reported apart.
        "decide": round((decided - searched) * 1000 - (out.extraction_ms or 0), 1),
        "extraction": out.extraction_ms,
        "act": round((done - decided) * 1000, 1),
        "total": round((done - start) * 1000, 1),
    }
    _log(user_id, query, retrieval, router, intent, out, result, timings)
    return result
