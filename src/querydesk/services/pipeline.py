"""
One chat message, end to end:

  open conversation? -> continue it (re-extract, merge, raise / ask / escalate)
  otherwise:  embed once -> retrieve + route (concurrently) -> classify -> decide -> act

`_act` and `_continue` are the only places a decision turns into side effects:
generating an answer, creating an SR, asking for missing details (which opens
a slot-filling conversation, docs/plan-day-6.md), or escalating. Raised SRs
and escalations are recorded in sr_requests. Every message logs one
structured decision line (PRD F10) carrying its `turn` in the conversation.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field

import anyio

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError
from querydesk.models.schemas import Decision, ResolutionType, Source
from querydesk.services import catalog, conversation, rag
from querydesk.services.conversation import Conversation
from querydesk.services.decision import extraction
from querydesk.services.decision.engine import (
    DecisionEngine,
    DecisionInput,
    DecisionOutput,
    get_engine,
)
from querydesk.services.decision.signals import Intent, classify
from querydesk.services.retrieval.query import embed_query_vectors
from querydesk.services.routing.router import RouterResult, route
from querydesk.services.state import postgres_store
from querydesk.services.ticketing import client as ticketing

logger = logging.getLogger(__name__)
decision_log = logging.getLogger("querydesk.decision")

# Only once the escalation is recorded in sr_requests can the reply say so.
ESCALATE_REPLY = (
    "I can't handle this one automatically, so I've flagged it for the service desk "
    "team. A person will pick it up."
)
# The record failed: don't promise that someone is already on it.
ESCALATE_FALLBACK_REPLY = (
    "I can't handle this one automatically. Please contact the service desk team "
    "directly so a person can pick it up."
)
STATUS_REPLY = (
    "I can't look up the status of existing requests yet — that's coming soon. "
    "You'll be notified automatically whenever one of your requests changes status."
)
CANCELLED_REPLY = "OK, I've cancelled that request. Nothing was raised."
DISCLAIMER = (
    '\n\nThis may not fully cover your case. If it doesn\'t, say "raise a request" '
    "and I'll open one for you."
)

# The whole message is a cancel ("cancel", "never mind", "stop it please").
# Anchored at both ends, so "stop the claims app" is never read as one.
_CANCEL = re.compile(
    r"^\s*(please\s+)?(cancel|never\s*mind|nvm|stop|forget\s+(it|that|about\s+it))"
    r"(\s+(it|that|this|the\s+request))?(\s+please)?[\s.!]*$",
    re.IGNORECASE,
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


def _base(out: DecisionOutput, confidence: float | None) -> dict:
    return dict(
        decision=out.decision,
        retrieval_confidence=confidence,
        category_id=out.category_id,
        requires_approval=out.requires_approval,
    )


def _ask_for(out: DecisionOutput, confidence: float | None) -> ChatResult:
    category = catalog.get(out.category_id)
    wanted = _join([_readable(f) for f in out.missing_fields])
    return ChatResult(
        # No "a/an" before the name: "a UAT…" vs "an Application…" can't be
        # decided from spelling, so the sentence avoids needing one.
        reply=f"I can raise this as a request ({category.name}). I just need the {wanted}.",
        resolution_type=ResolutionType.AUTO_SR,
        missing_fields=out.missing_fields,
        **_base(out, confidence),
    )


async def _already_open(
    user_id: str, out: DecisionOutput, confidence: float | None
) -> ChatResult | None:
    """
    F5: at most one open SR per user and category. Checked before asking for
    details as well as before raising, so nobody fills in a form for an SR
    they can't have. The idempotency key stays as the backstop for a retry
    that races this check.
    """
    open_srs = await ticketing.list_open_srs(user_id)
    existing = next((r for r in open_srs if r.category_id == out.category_id), None)
    if existing is None:
        return None
    category = catalog.get(out.category_id)
    return ChatResult(
        reply=(
            f"You already have SR {existing.id} open for {category.name} "
            f"({existing.status}). You'll be notified as it progresses."
        ),
        resolution_type=ResolutionType.AUTO_SR,
        sr_id=existing.id,
        **_base(out, confidence),
    )


async def _raise_sr(
    user_id: str, description: str, out: DecisionOutput, confidence: float | None
) -> ChatResult:
    category = catalog.get(out.category_id)
    if duplicate := await _already_open(user_id, out, confidence):
        return duplicate

    key = ticketing.idempotency_key(user_id, category.id, out.fields)
    record = await ticketing.create_sr(
        ticketing.SRCreate(
            user_id=user_id,
            category_id=category.id,
            description=description,
            requires_approval=bool(out.requires_approval),
            metadata=out.fields,
        ),
        key,
    )
    if record.replayed:
        # create_sr only replays an SR that is still open; a closed one
        # gets a new SR instead.
        reply = (
            f"You already have this open — it's SR {record.id} ({category.name}, {record.status})."
        )
    else:
        reply = f"I've raised SR {record.id} for {category.name}."
        try:
            await postgres_store.record_sr(
                ticket_id=record.id,
                user_id=user_id,
                category_id=category.id,
                details={"query": description, "fields": out.fields},
                status=record.status,
                requires_approval=bool(out.requires_approval),
                retrieval_confidence=confidence,
            )
        except PostgresStoreError as e:
            # The ticketing service is the source of truth: the user still
            # gets their SR number.
            logger.error("SR %s raised but not recorded in sr_requests: %s", record.id, e)
    reply += (
        " It needs approval before work starts; you'll be notified as it moves."
        if out.requires_approval
        else " You'll be notified as it progresses."
    )
    return ChatResult(
        reply=reply,
        resolution_type=ResolutionType.AUTO_SR,
        sr_id=record.id,
        **_base(out, confidence),
    )


async def _escalate(
    user_id: str, query: str, out: DecisionOutput, confidence: float | None
) -> ChatResult:
    """A fixed reply and no LLM call, recorded so the desk actually sees it."""
    try:
        await postgres_store.record_escalation(
            user_id=user_id,
            category_id=out.category_id,
            details={"query": query, "fields": out.fields, "missing": out.missing_fields},
            retrieval_confidence=confidence,
        )
        reply = ESCALATE_REPLY
    except PostgresStoreError as e:
        logger.error("Escalation not recorded in sr_requests: %s", e)
        reply = ESCALATE_FALLBACK_REPLY
    return ChatResult(
        reply=reply, resolution_type=ResolutionType.MANUAL_ESCALATION, **_base(out, confidence)
    )


async def _act(
    user_id: str, query: str, retrieval: rag.Retrieval, out: DecisionOutput
) -> ChatResult:
    confidence = retrieval.top_dense_score

    if out.decision in (Decision.ANSWER, Decision.ANSWER_WITH_DISCLAIMER):
        answer = await rag.generate(query, retrieval)
        reply = answer.reply
        if out.decision is Decision.ANSWER_WITH_DISCLAIMER:
            reply += DISCLAIMER
        return ChatResult(
            reply=reply,
            resolution_type=answer.resolution_type,
            sources=answer.sources,
            **_base(out, confidence),
        )

    if out.decision is Decision.RAISE_SR:
        return await _raise_sr(user_id, query, out, confidence)

    if out.decision is Decision.COLLECT_DETAILS:
        if duplicate := await _already_open(user_id, out, confidence):
            return duplicate
        # Open a conversation so the user's next message fills the gaps.
        await conversation.save(
            Conversation(
                user_id=user_id,
                category_id=out.category_id,
                original_query=query,
                fields=out.fields,
                missing=out.missing_fields,
                retrieval_confidence=confidence,
            )
        )
        return _ask_for(out, confidence)

    if out.decision is Decision.STATUS_LOOKUP:
        return ChatResult(
            reply=STATUS_REPLY, resolution_type=out.resolution_type, **_base(out, confidence)
        )

    return await _escalate(user_id, query, out, confidence)


def _log(
    user_id: str,
    query: str,
    intent: Intent,
    out: DecisionOutput,
    result: ChatResult | None,
    timings: dict,
    *,
    turn: int,
    retrieval: rag.Retrieval | None = None,
    router: RouterResult | None = None,
    confidence: float | None = None,
    error: str | None = None,
) -> None:
    """One line per message. A follow-up turn has no retrieval or router: it searched nothing."""
    if retrieval is not None:
        confidence = retrieval.top_dense_score
    decision_log.info(
        json.dumps(
            {
                "user_id": user_id,
                "query": query,
                # 0 for a fresh message, then 1, 2… for each reply in a conversation.
                "turn": turn,
                "top_dense_score": round(confidence, 4) if confidence is not None else None,
                "router_top_two": (
                    [[c.category_id, round(c.score, 4)] for c in router.top_two] if router else None
                ),
                "router_confident": router.confident if router else None,
                "intent": asdict(intent),
                "engine": out.engine,
                "decision": out.decision.value,
                "rule": out.rule,
                "category_id": out.category_id,
                "missing_fields": out.missing_fields,
                "requires_approval": out.requires_approval,
                "overrides": out.overrides,
                "extraction_ok": out.extraction_ok,
                "sr_id": result.sr_id if result else None,
                # Set when acting on the decision failed (generation, SR creation).
                "error": error,
                "latency_ms": timings,
            }
        )
    )


async def _continue(
    user_id: str, query: str, conv: Conversation, engine: DecisionEngine
) -> ChatResult | None:
    """
    The user's reply in an open slot-filling conversation. Returns None when
    the message doesn't answer it: the state is cleared and the caller
    handles the message as a fresh one.
    """
    start = time.perf_counter()
    intent = classify(query)
    turn = conv.follow_ups + 1
    category = catalog.get(conv.category_id)
    confidence = conv.retrieval_confidence

    if _CANCEL.match(query):
        await conversation.clear(user_id)
        out = DecisionOutput(
            Decision.CANCELLED, ResolutionType.AUTO_SR, 0, engine.name, conv.category_id
        )
        result = ChatResult(
            reply=CANCELLED_REPLY, resolution_type=out.resolution_type, **_base(out, confidence)
        )
        timings = {"total": round((time.perf_counter() - start) * 1000, 1)}
        _log(user_id, query, intent, out, result, timings, turn=turn, confidence=confidence)
        return result

    # Re-extract over the whole exchange, so "till 11 PM" keeps its context.
    transcript = conv.transcript(query)
    extract_start = time.perf_counter()
    found = await extraction.extract_fields(transcript, category)
    extraction_ms = round((time.perf_counter() - extract_start) * 1000, 1)

    # Values already collected are never overwritten.
    new = {k: v for k, v in found.fields.items() if k not in conv.fields}
    if not new:
        # Nothing in it answers what we asked: the user has moved on (a new
        # request, a question, a status ask).
        await conversation.clear(user_id)
        return None

    out = await engine.complete_sr(conv.category_id, {**conv.fields, **new})
    out.extraction_ok = found.ok
    out.extraction_ms = extraction_ms
    decided = time.perf_counter()

    result, error = None, None
    try:
        if out.decision is Decision.RAISE_SR:
            await conversation.clear(user_id)
            result = await _raise_sr(user_id, transcript, out, confidence)
        elif (
            out.decision is Decision.COLLECT_DETAILS and turn < settings.conversation_max_follow_ups
        ):
            conv.fields, conv.missing = out.fields, out.missing_fields
            conv.turns.append(query)
            conv.follow_ups = turn
            await conversation.save(conv)
            result = _ask_for(out, confidence)
        else:
            # A forced escalation (rule 6), or F4's cap: fields still missing
            # after the last follow-up we allow.
            if out.decision is Decision.COLLECT_DETAILS:
                out.overrides.append(
                    f"max_follow_ups: {settings.conversation_max_follow_ups} reached"
                )
                out.decision = Decision.ESCALATE
                out.resolution_type = ResolutionType.MANUAL_ESCALATION
            await conversation.clear(user_id)
            result = await _escalate(user_id, transcript, out, confidence)
        return result
    except Exception as e:
        error = type(e).__name__
        raise
    finally:
        done = time.perf_counter()
        timings = {
            "decide": round((decided - start) * 1000 - extraction_ms, 1),
            "extraction": extraction_ms,
            "act": round((done - decided) * 1000, 1),
            "total": round((done - start) * 1000, 1),
        }
        _log(
            user_id,
            query,
            intent,
            out,
            result,
            timings,
            turn=turn,
            confidence=confidence,
            error=error,
        )


async def handle_message(
    user_id: str, query: str, engine: DecisionEngine | None = None
) -> ChatResult:
    engine = engine or get_engine()
    conv = await conversation.load(user_id)
    if conv is not None:
        result = await _continue(user_id, query, conv, engine)
        if result is not None:
            return result
    return await _fresh(user_id, query, engine)


async def _fresh(user_id: str, query: str, engine: DecisionEngine) -> ChatResult:
    start = time.perf_counter()

    vectors = await embed_query_vectors(query)
    embedded = time.perf_counter()

    found: dict = {}

    async def _retrieve():
        found["retrieval"] = await rag.retrieve(query, vectors)

    async def _route():
        found["router"] = await route(query, vectors.dense)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_retrieve)
            tg.start_soon(_route)
    except BaseExceptionGroup as eg:
        # anyio wraps task errors in a group; unwrap so a VectorStoreError or
        # LLMError reaches the route's `except UpstreamError` (502), not a 500.
        raise eg.exceptions[0] from eg
    retrieval: rag.Retrieval = found["retrieval"]
    router: RouterResult = found["router"]
    searched = time.perf_counter()

    intent = classify(query)
    out = await engine.decide(
        DecisionInput(
            query=query,
            top_dense_score=retrieval.top_dense_score,
            has_hits=bool(retrieval.hits),
            router=router,
            intent=intent,
        )
    )
    decided = time.perf_counter()

    result, error = None, None
    try:
        result = await _act(user_id, query, retrieval, out)
        return result
    except Exception as e:
        # The failed messages are the ones most worth auditing: log the
        # decision anyway, then let the route map the error.
        error = type(e).__name__
        raise
    finally:
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
        _log(
            user_id,
            query,
            intent,
            out,
            result,
            timings,
            turn=0,
            retrieval=retrieval,
            router=router,
            error=error,
        )
