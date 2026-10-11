"""
RAG orchestration tests. The embed/search/generate calls are monkeypatched, so
these run offline and assert on behaviour rather than on model output.
"""

import pytest

from querydesk.models.schemas import ResolutionType
from querydesk.services import rag
from querydesk.services.retrieval.qdrant_store import RetrievedChunk
from querydesk.services.retrieval.query import QueryVectors


def _chunk(text="Commission is credited on the 7th.", title="Commission schedule", score=0.9):
    return RetrievedChunk(
        text=text,
        title=title,
        source_path="commission.md",
        source_url="https://kb.internal/commission",
        category_id="ops_commission_payout_query",
        chunk_index=0,
        score=score,
    )


@pytest.fixture
def stub_embeddings(monkeypatch):
    """Embedding is stubbed; retrieval is what each test varies."""

    async def fake_embed_query_vectors(text):
        return QueryVectors(dense=[0.1, 0.2], sparse=object())

    monkeypatch.setattr(rag, "embed_query_vectors", fake_embed_query_vectors)


@pytest.fixture
def llm_spy(monkeypatch):
    """Records whether the LLM was called, and with what."""
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return "Commission is credited on the 7th [1]."

    monkeypatch.setattr(rag, "chat_completion", fake_chat)
    return calls


def _stub_search(monkeypatch, hits, dense_score):
    async def fake_search(dense_vector, sparse_vector, top_k=None):
        return hits, dense_score

    monkeypatch.setattr(rag.qdrant_store, "hybrid_search", fake_search)


@pytest.mark.anyio
async def test_grounded_answer_returns_rag_with_sources(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [_chunk()], 0.82)

    result = await rag.answer("when is my commission credited?")

    assert result.resolution_type is ResolutionType.RAG
    assert result.reply == "Commission is credited on the 7th [1]."
    assert len(result.sources) == 1
    assert result.sources[0].title == "Commission schedule"
    assert result.sources[0].source_url == "https://kb.internal/commission"
    assert result.sources[0].category_id == "ops_commission_payout_query"


@pytest.mark.anyio
async def test_no_hits_escalates_without_calling_the_llm(monkeypatch, stub_embeddings, llm_spy):
    """
    The core guardrail: with nothing retrieved we must not ask the model to
    answer anyway. Asserting the LLM was never called is the real test — the
    resolution type alone would pass even if we'd generated and discarded.
    """
    _stub_search(monkeypatch, [], 0.1)

    result = await rag.answer("what is the capital of France")

    assert result.resolution_type is ResolutionType.MANUAL_ESCALATION
    assert result.sources == []
    assert result.reply == rag.NO_CONTEXT_REPLY
    assert llm_spy == []


@pytest.mark.anyio
async def test_confidence_is_the_dense_score_not_a_fused_score(
    monkeypatch, stub_embeddings, llm_spy
):
    """
    PRD Gap #2. The fused hit score (0.5 here, a rank-derived RRF value) must
    never become the confidence number; the dense similarity (0.82) must.
    """
    _stub_search(monkeypatch, [_chunk(score=0.5)], 0.82)

    result = await rag.answer("when is my commission credited?")

    assert result.retrieval_confidence == 0.82
    assert result.sources[0].score == 0.5


@pytest.mark.anyio
async def test_confidence_is_reported_even_when_escalating(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [], 0.17)

    result = await rag.answer("unrelated question")

    # Day 3 calibration needs the near-miss scores, not just the hits.
    assert result.retrieval_confidence == 0.17


@pytest.mark.anyio
async def test_context_is_numbered_and_sent_to_the_llm(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(
        monkeypatch,
        [_chunk(text="First article.", title="A"), _chunk(text="Second article.", title="B")],
        0.8,
    )

    await rag.answer("a question")

    system_prompt = llm_spy[0]["messages"][0]["content"]
    assert "[1] A" in system_prompt
    assert "[2] B" in system_prompt
    assert "First article." in system_prompt
    assert "Second article." in system_prompt


@pytest.mark.anyio
async def test_system_prompt_forbids_outside_knowledge(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [_chunk()], 0.8)

    await rag.answer("a question")

    system_prompt = llm_spy[0]["messages"][0]["content"].lower()
    assert "only" in system_prompt
    assert "never guess" in system_prompt


@pytest.mark.anyio
async def test_user_query_is_passed_through_verbatim(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [_chunk()], 0.8)

    await rag.answer("when is my commission credited?")

    user_message = llm_spy[0]["messages"][1]
    assert user_message["role"] == "user"
    assert user_message["content"] == "when is my commission credited?"


@pytest.mark.anyio
async def test_generation_uses_low_temperature(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [_chunk()], 0.8)

    await rag.answer("a question")

    assert llm_spy[0]["temperature"] == 0.2


@pytest.mark.anyio
async def test_snippet_is_truncated_for_the_api_response(monkeypatch, stub_embeddings, llm_spy):
    _stub_search(monkeypatch, [_chunk(text="x" * 1000)], 0.8)

    result = await rag.answer("a question")

    # The citation carries a preview, not the whole chunk.
    assert len(result.sources[0].snippet) == 300


@pytest.mark.anyio
async def test_precomputed_vectors_skip_embedding(monkeypatch, llm_spy):
    """The router shares the dense vector; answer() must not embed a second time."""

    async def must_not_embed(text):
        raise AssertionError("embedded twice")

    monkeypatch.setattr(rag, "embed_query_vectors", must_not_embed)
    seen = {}

    async def fake_search(dense_vector, sparse_vector, top_k=None):
        seen["dense"] = dense_vector
        return [_chunk()], 0.8

    monkeypatch.setattr(rag.qdrant_store, "hybrid_search", fake_search)

    await rag.answer("q", vectors=QueryVectors(dense=[0.5], sparse=object()))

    assert seen["dense"] == [0.5]


@pytest.mark.anyio
async def test_retrieve_does_not_generate(monkeypatch, stub_embeddings, llm_spy):
    """The decision layer calls retrieve() for every message; it must never hit the LLM."""
    _stub_search(monkeypatch, [_chunk()], 0.8)

    retrieval = await rag.retrieve("q")

    assert retrieval.top_dense_score == 0.8
    assert len(retrieval.hits) == 1
    assert llm_spy == []


@pytest.mark.anyio
async def test_generate_with_no_hits_escalates_without_the_llm(llm_spy):
    result = await rag.generate("q", rag.Retrieval(hits=[], top_dense_score=0.2))

    assert result.resolution_type is ResolutionType.MANUAL_ESCALATION
    assert llm_spy == []
