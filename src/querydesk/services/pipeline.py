"""
One chat message, end to end:

  open conversation? -> yes/no to a pending SR | extract from the reply -> merge
                        -> confirm / ask again / escalate
  small talk ("hi", "thanks")? -> a fixed reply, no search, nothing recorded
  otherwise:  embed once -> retrieve + route (concurrently) -> classify -> decide -> act

An SR is never raised on the first complete message: the user sees what will
be raised and says yes (stage "confirm" of the conversation). A "no" or a
cancel raises nothing, and a reply that corrects a detail re-confirms.

A reply that states no field is judged as a fresh message first: if that
would only escalate (rule 5, it makes no sense on its own) it counts as an
unhelpful answer to the open conversation; anything else (a question, a new
request) closes the conversation and is handled normally.

`_act` and `_advance` are the only places a decision turns into side effects:
generating an answer, creating an SR, asking for missing details (which opens
a slot-filling conversation, docs/plan-day-6.md), or escalating. Raised SRs
and escalations are recorded in sr_requests. Every message logs one
structured decision line (PRD F10) carrying its `turn` in the conversation,
and the same record is written to decision_logs in the background.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import anyio

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError
from querydesk.models.schemas import Decision, ResolutionType, Source
from querydesk.services import catalog, conversation, history, query_rewrite, rag, status
from querydesk.services.conversation import Conversation
from querydesk.services.decision import extraction, smalltalk
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
CANCELLED_REPLY = "OK, I've cancelled that request. Nothing was raised."
NOTHING_TO_CANCEL_REPLY = "There's no open request to cancel."
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

# The whole message is a yes / no to "Shall I raise this?".
_YES = re.compile(
    r"^\s*(yes+|yeah|yep|yup|y|sure|ok(ay)?|confirm(ed)?|go\s+ahead|go\s+for\s+it|"
    r"please\s+do|do\s+it|proceed|raise\s+(it|this)|sounds\s+good|correct|that'?s\s+right)"
    r"(\s+please)?[\s.!]*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(no+|nope|nah|n|don'?t|do\s+not|not\s+now|no\s+thanks?)[\s.!]*$", re.IGNORECASE
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

    Reported as RAISE_SR with the existing SR's id, like an idempotent replay:
    the user's request maps to that SR, not to a form.
    """
    open_srs = await ticketing.list_open_srs(user_id)
    existing = next((r for r in open_srs if r.category_id == out.category_id), None)
    if existing is None:
        return None
    category = catalog.get(out.category_id)
    out.overrides.append(f"duplicate_of: SR {existing.id}")
    out.decision = Decision.RAISE_SR
    return ChatResult(
        reply=(
            f"You already have SR {existing.id} open for {category.name} "
            f"({existing.status}). You'll be notified as it progresses."
        ),
        resolution_type=ResolutionType.AUTO_SR,
        sr_id=existing.id,
        **_base(out, confidence),
    )


def _confirm_reply(out: DecisionOutput) -> str:
    category = catalog.get(out.category_id)
    lines = [f"Here's what I'll raise ({category.name}):"]
    lines += [f"- {_readable(name)}: {value}" for name, value in out.fields.items()]
    if out.requires_approval:
        lines.append("It will need approval before work starts.")
    lines.append('Shall I go ahead? Reply "yes" to raise it, or "no" to cancel.')
    return "\n".join(lines)


async def _propose_sr(
    conv: Conversation, out: DecisionOutput, confidence: float | None, reply: str | None = None
) -> ChatResult:
    """
    The SR is complete: show it and wait for a yes. Nothing is created here.
    A duplicate is reported instead (F5), so nobody confirms an SR they
    can't have. `reply` is the user's message when it added to the request.
    """
    if duplicate := await _already_open(conv.user_id, out, confidence):
        await conversation.clear(conv.user_id)
        return duplicate
    if reply is not None:
        conv.turns.append(reply)
        conv.follow_ups += 1
    conv.fields, conv.missing, conv.stage = out.fields, [], "confirm"
    await conversation.save(conv)
    out.decision = Decision.CONFIRM_SR
    return ChatResult(
        reply=_confirm_reply(out),
        resolution_type=ResolutionType.AUTO_SR,
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
    user_id: str,
    query: str,
    out: DecisionOutput,
    confidence: float | None,
    replies: tuple[str, str] = (ESCALATE_REPLY, ESCALATE_FALLBACK_REPLY),
) -> ChatResult:
    """
    A fixed reply and no LLM call, recorded so the desk actually sees it.
    `replies` is (recorded, not recorded): only a recorded escalation may say
    it has been flagged.
    """
    recorded, fallback = replies
    try:
        await postgres_store.record_escalation(
            user_id=user_id,
            category_id=out.category_id,
            details={"query": query, "fields": out.fields, "missing": out.missing_fields},
            retrieval_confidence=confidence,
        )
        reply = recorded
    except PostgresStoreError as e:
        logger.error("Escalation not recorded in sr_requests: %s", e)
        reply = fallback
    # No SR, so nothing to approve: don't echo the category's approval rule.
    return ChatResult(
        reply=reply,
        resolution_type=ResolutionType.MANUAL_ESCALATION,
        **{**_base(out, confidence), "requires_approval": None},
    )


def _cap_replies(missing: list[str]) -> tuple[str, str]:
    """F4's cap: say why this went to a person."""
    still = (
        f"I still don't have the {_join([_readable(f) for f in missing])} after a couple of tries"
    )
    return (
        f"{still}, so I've flagged this for the service desk team. A person will pick it up.",
        f"{still}. Please contact the service desk team directly so a person can pick it up.",
    )


async def _act(
    user_id: str,
    query: str,
    retrieval: rag.Retrieval | None,
    out: DecisionOutput,
    answer_query: str | None = None,
) -> ChatResult:
    confidence = retrieval.top_dense_score if retrieval is not None else None

    if out.decision in (Decision.ANSWER, Decision.ANSWER_WITH_DISCLAIMER):
        # The answer is for the question as searched (a rewritten follow-up),
        # but every other path acts on what the user actually wrote.
        answer = await rag.generate(answer_query or query, retrieval)
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
        conv = Conversation(
            user_id=user_id,
            category_id=out.category_id,
            original_query=query,
            retrieval_confidence=confidence,
        )
        return await _propose_sr(conv, out, confidence)

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
            reply=await status.answer(user_id, query),
            resolution_type=out.resolution_type,
            **_base(out, confidence),
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
    """
    One record per message, logged and written to decision_logs. A follow-up
    that stated a field searched nothing, so it has no retrieval or router.
    """
    if retrieval is not None and confidence is None:
        confidence = retrieval.top_dense_score
    record = {
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
    decision_log.info(json.dumps(record))
    _record_decision(record)


# Decision rows being written. The event loop keeps only weak references to
# tasks, so this holds each one until it finishes.
_decision_writes: set[asyncio.Task] = set()


def _record_decision(record: dict) -> None:
    """
    Write the decision to decision_logs in the background: the Neon round trip
    (~100–300 ms) never adds to the reply's latency, and a failed write is
    logged, never raised — the log line above already has the record. No
    POSTGRES_DSN means nothing to write to.
    """
    if not settings.postgres_dsn:
        return
    write = postgres_store.record_decision(record, at=datetime.now(UTC))
    task = asyncio.get_running_loop().create_task(write)
    _decision_writes.add(task)
    task.add_done_callback(_decision_written)


def _decision_written(task: asyncio.Task) -> None:
    _decision_writes.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("decision_logs write failed: %s", task.exception())


async def drain_decision_writes() -> None:
    """Wait for the decision rows still being written (app shutdown, tests)."""
    if _decision_writes:
        await asyncio.gather(*_decision_writes, return_exceptions=True)


async def _advance(
    user_id: str,
    query: str,
    conv: Conversation,
    out: DecisionOutput,
    *,
    confirmed: bool = False,
) -> ChatResult:
    """
    One reply in an open conversation, decided (`out` from complete_sr): ask
    to confirm a complete SR (raise it once `confirmed`), ask again for
    what's still missing, or escalate — forced by rule 6, or F4's cap after
    the last follow-up we allow. The escalation carries the conversation's
    category, fields and transcript, so the desk has them.
    """
    turn = conv.follow_ups + 1
    transcript = conv.transcript(query)
    confidence = conv.retrieval_confidence

    if out.decision is Decision.RAISE_SR:
        if not confirmed:
            return await _propose_sr(conv, out, confidence, reply=query)
        await conversation.clear(user_id)
        return await _raise_sr(user_id, conv.transcript_so_far(), out, confidence)

    if out.decision is Decision.COLLECT_DETAILS and turn < settings.conversation_max_follow_ups:
        conv.fields, conv.missing = out.fields, out.missing_fields
        conv.turns.append(query)
        conv.follow_ups = turn
        await conversation.save(conv)
        return _ask_for(out, confidence)

    await conversation.clear(user_id)
    if out.decision is not Decision.COLLECT_DETAILS:  # rule 6 forced it
        return await _escalate(user_id, transcript, out, confidence)
    out.overrides.append(f"max_follow_ups: {settings.conversation_max_follow_ups} reached")
    out.decision = Decision.ESCALATE
    out.resolution_type = ResolutionType.MANUAL_ESCALATION
    return await _escalate(
        user_id, transcript, out, confidence, replies=_cap_replies(out.missing_fields)
    )


async def _logged_advance(
    user_id: str,
    query: str,
    conv: Conversation,
    out: DecisionOutput,
    intent: Intent,
    timings: dict,
    start: float,
    *,
    turn: int,
    router: RouterResult | None = None,
    confirmed: bool = False,
) -> ChatResult:
    decided = time.perf_counter()
    result, error = None, None
    try:
        result = await _advance(user_id, query, conv, out, confirmed=confirmed)
        return result
    except Exception as e:
        error = type(e).__name__
        raise
    finally:
        done = time.perf_counter()
        timings = {
            **timings,
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
            router=router,
            confidence=conv.retrieval_confidence,
            error=error,
        )


async def _continue(
    user_id: str, query: str, conv: Conversation, engine: DecisionEngine
) -> ChatResult | None:
    """
    The user's reply in an open slot-filling conversation. Returns None when
    the reply states no field at all; the caller then judges it as a fresh
    message (see the module docstring).
    """
    start = time.perf_counter()
    intent = classify(query)
    turn = conv.follow_ups + 1

    confirming = conv.stage == "confirm"
    if _CANCEL.match(query) or (confirming and _NO.match(query)):
        await conversation.clear(user_id)
        out = DecisionOutput(
            Decision.CANCELLED, ResolutionType.AUTO_SR, 0, engine.name, conv.category_id
        )
        result = ChatResult(
            reply=CANCELLED_REPLY,
            resolution_type=out.resolution_type,
            **_base(out, conv.retrieval_confidence),
        )
        timings = {"total": round((time.perf_counter() - start) * 1000, 1)}
        _log(
            user_id,
            query,
            intent,
            out,
            result,
            timings,
            turn=turn,
            confidence=conv.retrieval_confidence,
        )
        return result

    if confirming and _YES.match(query):
        out = await engine.complete_sr(conv.category_id, conv.fields)
        out.overrides.append("confirmed")
        timings = {"decide": round((time.perf_counter() - start) * 1000, 1), "extraction": None}
        return await _logged_advance(
            user_id, query, conv, out, intent, timings, start, turn=turn, confirmed=True
        )

    # Only the reply is a source of values; the original request is context.
    extract_start = time.perf_counter()
    found = await extraction.extract_follow_up(
        conv.original_query, query, catalog.get(conv.category_id), conv.fields, conv.missing
    )
    extraction_ms = round((time.perf_counter() - extract_start) * 1000, 1)
    if not found.fields:
        return None

    # A value the reply states wins: it is either new or a correction
    # ("it's UAT-3" after a vague "UAT"). Grounding guarantees it was said.
    corrected = [k for k, v in found.fields.items() if k in conv.fields and conv.fields[k] != v]
    out = await engine.complete_sr(conv.category_id, {**conv.fields, **found.fields})
    out.overrides.extend(f"corrected: {k}" for k in corrected)
    out.extraction_ok = found.ok
    out.extraction_ms = extraction_ms
    timings = {
        "decide": round((time.perf_counter() - start) * 1000 - extraction_ms, 1),
        "extraction": extraction_ms,
    }
    return await _logged_advance(user_id, query, conv, out, intent, timings, start, turn=turn)


@dataclass
class _Fresh:
    """
    A fresh message, decided but not yet acted on. A status question searched
    nothing, so it has no retrieval or router.
    """

    retrieval: rag.Retrieval | None
    router: RouterResult | None
    intent: Intent
    out: DecisionOutput
    start: float
    timings: dict
    # What was embedded and searched: `query`, or its rewrite as a standalone
    # question. Intent, extraction, the SR text and logs always use `query`.
    search_query: str = ""


async def _decide_fresh(
    query: str, engine: DecisionEngine, search_query: str | None = None
) -> _Fresh:
    start = time.perf_counter()
    intent = classify(query)
    search_query = search_query or query

    if intent.is_status:
        # Rule 1 needs nothing but the intent, so a status question skips the
        # embedding (~2 s, a Fireworks call) and the search entirely.
        out = await engine.decide(
            DecisionInput(
                query=query,
                top_dense_score=0.0,
                has_hits=False,
                router=RouterResult(),
                intent=intent,
            )
        )
        decided = round((time.perf_counter() - start) * 1000, 1)
        timings = {"embed": 0.0, "search": 0.0, "decide": decided, "extraction": None}
        return _Fresh(None, None, intent, out, start, timings)

    vectors = await embed_query_vectors(search_query)
    embedded = time.perf_counter()

    found: dict = {}

    async def _retrieve():
        found["retrieval"] = await rag.retrieve(search_query, vectors)

    async def _route():
        found["router"] = await route(search_query, vectors.dense)

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
    timings = {
        "embed": round((embedded - start) * 1000, 1),
        "search": round((searched - embedded) * 1000, 1),
        # The decision layer's own cost; the LLM extraction is reported apart.
        "decide": round((decided - searched) * 1000 - (out.extraction_ms or 0), 1),
        "extraction": out.extraction_ms,
    }
    return _Fresh(retrieval, router, intent, out, start, timings, search_query)


async def _act_fresh(user_id: str, query: str, fresh: _Fresh) -> ChatResult:
    acting = time.perf_counter()
    result, error = None, None
    try:
        result = await _act(user_id, query, fresh.retrieval, fresh.out, fresh.search_query)
        return result
    except Exception as e:
        # The failed messages are the ones most worth auditing: log the
        # decision anyway, then let the route map the error.
        error = type(e).__name__
        raise
    finally:
        done = time.perf_counter()
        timings = {
            **fresh.timings,
            "act": round((done - acting) * 1000, 1),
            "total": round((done - fresh.start) * 1000, 1),
        }
        _log(
            user_id,
            query,
            fresh.intent,
            fresh.out,
            result,
            timings,
            turn=0,
            retrieval=fresh.retrieval,
            router=fresh.router,
            error=error,
        )


def _pending_nudge(conv: Conversation) -> str:
    """Small talk doesn't close an open request: say what it is waiting for."""
    category = catalog.get(conv.category_id)
    if conv.stage == "confirm":
        return (
            f' Your {category.name} request is waiting: reply "yes" to raise it or "no" to cancel.'
        )
    wanted = _join([_readable(f) for f in conv.missing])
    return (
        f' I\'m still waiting for the {wanted} for your {category.name} request, or say "cancel".'
    )


def _smalltalk(
    user_id: str,
    query: str,
    kind: smalltalk.Kind,
    conv: Conversation | None,
    engine: DecisionEngine,
) -> ChatResult:
    """
    Hello, thanks, "what can you do?": a fixed reply. No embedding, search,
    LLM call or sr_requests row, and an open conversation is left alone.
    """
    out = DecisionOutput(
        Decision.SMALLTALK,
        ResolutionType.CONVERSATION,
        0,
        engine.name,
        conv.category_id if conv else None,
    )
    reply = smalltalk.reply(kind) + (_pending_nudge(conv) if conv else "")
    result = ChatResult(reply=reply, resolution_type=out.resolution_type, **_base(out, None))
    out.overrides.append(f"smalltalk: {kind.value}")
    _log(
        user_id,
        query,
        classify(query),
        out,
        result,
        {"total": 0.0},
        turn=conv.follow_ups + 1 if conv else 0,
    )
    return result


async def _search_text(user_id: str, query: str) -> str:
    """`query` read in the light of the recent chat, when that is switched on and needed."""
    if not settings.history_rewrite_enabled or not query_rewrite.has_reference(query):
        return query
    return await query_rewrite.standalone(query, await history.load(user_id))


def _record_history(user_id: str, query: str, result: ChatResult) -> None:
    """Remember the exchange in the background: never adds to the reply's latency."""
    task = asyncio.get_running_loop().create_task(
        history.append(user_id, query, result.reply, result.decision.value)
    )
    _decision_writes.add(task)
    task.add_done_callback(_decision_written)


async def handle_message(
    user_id: str, query: str, engine: DecisionEngine | None = None
) -> ChatResult:
    result = await _handle_message(user_id, query, engine)
    _record_history(user_id, query, result)
    return result


async def _handle_message(
    user_id: str, query: str, engine: DecisionEngine | None = None
) -> ChatResult:
    engine = engine or get_engine()
    conv = await conversation.load(user_id)

    kind = smalltalk.classify(query)
    answering_prompt = (
        conv is not None and conv.stage == "confirm" and (_YES.match(query) or _NO.match(query))
    )
    if kind is not None and not answering_prompt:
        return _smalltalk(user_id, query, kind, conv, engine)

    if conv is None and _CANCEL.match(query):
        # Nothing open: don't search, escalate or record a bare "cancel".
        out = DecisionOutput(Decision.CANCELLED, ResolutionType.AUTO_SR, 0, engine.name)
        result = ChatResult(
            reply=NOTHING_TO_CANCEL_REPLY, resolution_type=out.resolution_type, **_base(out, None)
        )
        _log(user_id, query, classify(query), out, result, {"total": 0.0}, turn=0)
        return result

    if conv is not None:
        result = await _continue(user_id, query, conv, engine)
        if result is not None:
            return result

    # Only with nothing open: a reply in a conversation is about that request.
    search_query = await _search_text(user_id, query) if conv is None else query
    fresh = await _decide_fresh(query, engine, search_query)
    if search_query != query:
        fresh.out.overrides.append("rewritten: searched as a standalone question")
    if conv is None:
        return await _act_fresh(user_id, query, fresh)

    if fresh.out.decision is Decision.ESCALATE and fresh.out.rule == 5:
        # Meaningless on its own ("not sure", "hmm"): an unhelpful answer to
        # the open conversation, not a new request. Ask again, or escalate
        # with the conversation's context at the cap.
        out = await engine.complete_sr(conv.category_id, conv.fields)
        out.overrides.append("unanswered: the reply stated no field")
        return await _logged_advance(
            user_id,
            query,
            conv,
            out,
            fresh.intent,
            fresh.timings,
            fresh.start,
            turn=conv.follow_ups + 1,
            router=fresh.router,
        )

    # A question, a status ask or a new request: the user has moved on.
    await conversation.clear(user_id)
    return await _act_fresh(user_id, query, fresh)
