"""
Retrieval-augmented answering: hybrid search, then a grounded answer that
cites the articles it used.

This module answers informational questions. It does not decide *whether* a
message should be answered at all — that's the decision layer, which calls
retrieve() for every message (it needs the score) and generate() only on the
ANSWER paths. Keep routing rules out of here.
"""

from dataclasses import dataclass, field

from querydesk.models.schemas import ResolutionType, Source
from querydesk.services.llm.fireworks import chat_completion
from querydesk.services.retrieval import qdrant_store
from querydesk.services.retrieval.query import QueryVectors, embed_query_vectors

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
    return "\n\n".join(f"[{i}] {hit.title}\n{hit.text}" for i, hit in enumerate(hits, start=1))


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


@dataclass
class Retrieval:
    """What hybrid search found. `top_dense_score` is the confidence signal (PRD Gap #2)."""

    hits: list[qdrant_store.RetrievedChunk]
    top_dense_score: float


async def retrieve(query: str, vectors: QueryVectors | None = None) -> Retrieval:
    """
    Hybrid search only — no generation. The decision layer needs the score
    before it decides whether to answer at all.

    Pass `vectors` when the caller has already embedded the query (the router
    shares the same dense vector), so it isn't embedded twice.
    """
    if vectors is None:
        vectors = await embed_query_vectors(query)

    hits, top_dense_score = await qdrant_store.hybrid_search(
        dense_vector=vectors.dense, sparse_vector=vectors.sparse
    )
    return Retrieval(hits=hits, top_dense_score=top_dense_score)


async def generate(query: str, retrieval: Retrieval) -> RagResult:
    """
    A grounded, cited answer from already-retrieved chunks. Returns
    MANUAL_ESCALATION without calling the LLM when there is nothing to ground on.
    """
    if not retrieval.hits:
        # No LLM call at all. Cheap, deterministic, and it's the guardrail
        # against answering with something invented.
        return RagResult(
            reply=NO_CONTEXT_REPLY,
            resolution_type=ResolutionType.MANUAL_ESCALATION,
            sources=[],
            retrieval_confidence=retrieval.top_dense_score,
        )

    reply = await chat_completion(
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(context=_build_context(retrieval.hits)),
            },
            {"role": "user", "content": query},
        ],
        temperature=0.2,
    )

    return RagResult(
        reply=reply,
        resolution_type=ResolutionType.RAG,
        sources=_to_sources(retrieval.hits),
        retrieval_confidence=retrieval.top_dense_score,
    )
