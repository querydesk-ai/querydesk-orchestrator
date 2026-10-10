"""
Semantic router (PRD F3): which of the 10 categories is this message about?

Compares the query's dense vector against each category's prose description
and returns the top two. Embedding similarity is cheap and needs no LLM, so it
decides on its own whenever the winner is clear. Only when the top two are
within `router_tie_margin` of each other does an LLM tie-break pick between
them.

The router reports; it does not decide. Whether a confident category becomes
an SR, a follow-up question or an answer is the decision layer's job (Day 4).
"""

from dataclasses import dataclass, field

from querydesk.core.config import settings
from querydesk.services.llm.fireworks import FireworksError, chat_completion
from querydesk.services.retrieval import qdrant_store
from querydesk.services.retrieval.qdrant_store import CategoryHit

TIE_BREAK_PROMPT = """You route internal service desk messages to a category.

Message: {query}

Candidate categories:
{candidates}

Reply with exactly one category id from the list above and nothing else.
If neither category fits the message, reply with: none"""


@dataclass
class RouterResult:
    # Best first. Always the raw similarity ranking, even after a tie-break,
    # so decision_logs.router_top_two records what the embeddings said.
    top_two: list[CategoryHit] = field(default_factory=list)
    # The chosen category, or None when the router isn't confident.
    category_id: str | None = None
    confident: bool = False
    tie_broken: bool = False

    @property
    def margin(self) -> float | None:
        if len(self.top_two) < 2:
            return None
        return self.top_two[0].score - self.top_two[1].score


async def route(query: str, dense_vector: list[float]) -> RouterResult:
    """
    Route a query. `dense_vector` must be the query embedding from
    embed_query_vectors(), shared with retrieval rather than recomputed.
    """
    hits = await qdrant_store.search_categories(dense_vector, limit=2)
    result = RouterResult(top_two=hits)

    if not hits or hits[0].score < settings.router_min_score:
        # Nothing is close enough: outside the 10 categories -> escalate.
        return result

    margin = result.margin
    if margin is None or margin >= settings.router_tie_margin:
        result.category_id = hits[0].category_id
        result.confident = True
        return result

    # Too close to call on similarity alone.
    if not settings.router_tie_break_enabled:
        return result

    winner = await _tie_break(query, hits)
    if winner is not None:
        result.category_id = winner
        result.confident = True
        result.tie_broken = True
    return result


async def _tie_break(query: str, candidates: list[CategoryHit]) -> str | None:
    """
    Ask the LLM to choose between the top candidates. Returns a candidate id,
    or None — on an unrecognised reply or a Fireworks failure — so the router
    falls back to "not confident" (escalate) instead of guessing.
    """
    listing = "\n".join(f"- {c.category_id}: {c.description}" for c in candidates)
    try:
        reply = await chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": TIE_BREAK_PROMPT.format(query=query, candidates=listing),
                }
            ],
            temperature=0.0,
        )
    except FireworksError:
        # A degraded tie-break must not fail the whole message: escalating to
        # a person is a safe answer, a 502 is not.
        return None

    choice = reply.strip().strip("`'\".").lower()
    valid = {c.category_id for c in candidates}
    return choice if choice in valid else None
