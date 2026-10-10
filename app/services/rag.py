"""
Retrieval-augmented answering: hybrid search, then a grounded answer that
cites the articles it used.

This module answers informational questions. It does not decide *whether* a
message should be answered at all — that's the decision layer (PRD Day 4),
which calls this function for the ANSWER paths and bypasses it for the SR
paths. Keep routing rules out of here.
"""
from dataclasses import dataclass, field

import anyio

from app.models.schemas import ResolutionType, Source
from app.services import qdrant_store, sparse
from app.services.fireworks_client import chat_completion, embed_query

NO_CONTEXT_REPLY = (
    "I don't have information on that in the knowledge base. "
    "I can raise a request for a person to look into it if you'd like."
)

SYSTEM_PROMPT = """You are QueryDesk, an internal service desk assistant for an insurance company.

Answer the user's question using ONLY the numbered context articles below. Rules:
- If the context does not contain the answer, say so plainly. Never guess or use outside knowledge.
- Cite the articles you used by their number, like [1] or [2].
- Be concise and direct. Prefer the exact steps, timelines or figures from the articles.
- If the context only partially covers the question, answer what it covers and say what it doesn't.

Context articles:
{context}"""


@dataclass
class RagResult:
    reply: str
    resolution_type: ResolutionType
    sources: list[Source] = field(default_factory=list)
    retrieval_confidence: float | None = None


def _build_context(hits: list[qdrant_store.RetrievedChunk]) -> str:
    return "\n\n".join(
        f"[{i}] {hit.title}\n{hit.text}" for i, hit in enumerate(hits, start=1)
    )


def _to_sources(hits: list[qdrant_store.RetrievedChunk]) -> list[Source]:
    return [
        Source(
            title=hit.title,
            source_path=hit.source_path,
            source_url=hit.source_url,
            category_id=hit.category_id,
            snippet=hit.text[:300],
            score=hit.score,
        )
        for hit in hits
    ]


async def answer(query: str) -> RagResult:
    """
    Retrieve and answer. Returns RAG with citations when the knowledge base
    covers the question, MANUAL_ESCALATION when it doesn't.
    """
    # Dense needs the network, sparse needs the CPU — overlap them.
    async with anyio.create_task_group() as tg:
        holder: dict = {}

        async def _dense():
            holder["dense"] = await embed_query(query)

        async def _sparse():
            holder["sparse"] = await sparse.sparse_embed_query_async(query)

        tg.start_soon(_dense)
        tg.start_soon(_sparse)

    hits, top_dense_score = await qdrant_store.hybrid_search(
        dense_vector=holder["dense"], sparse_vector=holder["sparse"]
    )

    if not hits:
        # No LLM call at all. Cheap, deterministic, and it's the guardrail
        # against answering with something invented.
        return RagResult(
            reply=NO_CONTEXT_REPLY,
            resolution_type=ResolutionType.MANUAL_ESCALATION,
            sources=[],
            retrieval_confidence=top_dense_score,
        )

    reply = await chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(context=_build_context(hits))},
            {"role": "user", "content": query},
        ],
        temperature=0.2,
    )

    return RagResult(
        reply=reply,
        resolution_type=ResolutionType.RAG,
        sources=_to_sources(hits),
        retrieval_confidence=top_dense_score,
    )
