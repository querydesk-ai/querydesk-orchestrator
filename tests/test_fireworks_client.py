"""
Fireworks client tests. Fully offline — respx intercepts the HTTP calls, so
these run in CI with no API key.
"""
import httpx
import pytest
import respx

from app.config import settings
from app.services import fireworks_client as fw

EMBED_URL = f"{fw.FIREWORKS_BASE_URL}/embeddings"
CHAT_URL = f"{fw.FIREWORKS_BASE_URL}/chat/completions"


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    """Every test here assumes a configured key; the missing-key case sets its own."""
    monkeypatch.setattr(settings, "fireworks_api_key", "test-key")


def _embed_response(request: httpx.Request) -> httpx.Response:
    """Echo back one vector per input, so tests can assert on ordering."""
    import json

    inputs = json.loads(request.content)["input"]
    return httpx.Response(
        200, json={"data": [{"embedding": [float(i), 0.0]} for i in range(len(inputs))]}
    )


@pytest.mark.anyio
async def test_missing_api_key_raises_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "fireworks_api_key", "")

    with pytest.raises(fw.FireworksError, match="FIREWORKS_API_KEY"):
        await fw.embed_documents(["anything"])


@pytest.mark.anyio
@respx.mock
async def test_embed_documents_applies_the_document_prefix():
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    await fw.embed_documents(["how do I reset my password"])

    sent = route.calls.last.request
    import json

    assert json.loads(sent.content)["input"] == [
        "search_document: how do I reset my password"
    ]


@pytest.mark.anyio
@respx.mock
async def test_embed_query_applies_the_query_prefix():
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    await fw.embed_query("how do I reset my password")

    import json

    assert json.loads(route.calls.last.request.content)["input"] == [
        "search_query: how do I reset my password"
    ]


@pytest.mark.anyio
@respx.mock
async def test_document_and_query_prefixes_differ():
    """
    The whole point of the asymmetric model: mixing the two prefixes across
    the sides of a comparison degrades similarity, so they must not converge.
    """
    assert fw.DOCUMENT_PREFIX != fw.QUERY_PREFIX


@pytest.mark.anyio
@respx.mock
async def test_embed_query_returns_a_single_vector():
    respx.post(EMBED_URL).mock(side_effect=_embed_response)

    vector = await fw.embed_query("hello")

    assert vector == [0.0, 0.0]


@pytest.mark.anyio
@respx.mock
async def test_embedding_requests_are_batched(monkeypatch):
    monkeypatch.setattr(settings, "embed_batch_size", 2)
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    vectors = await fw.embed_text([f"text {i}" for i in range(5)])

    # 5 items at batch size 2 -> 3 requests, and every vector comes back.
    assert route.call_count == 3
    assert len(vectors) == 5


@pytest.mark.anyio
@respx.mock
async def test_batches_preserve_input_order(monkeypatch):
    monkeypatch.setattr(settings, "embed_batch_size", 2)
    respx.post(EMBED_URL).mock(side_effect=_embed_response)

    vectors = await fw.embed_text(["a", "b", "c"])

    # Each mocked batch numbers its vectors from 0, so batches [a,b] and [c]
    # concatenate to 0,1 | 0 — proves batches are appended in order rather
    # than interleaved or overwritten.
    assert [v[0] for v in vectors] == [0.0, 1.0, 0.0]


@pytest.mark.anyio
async def test_empty_input_makes_no_request():
    with respx.mock:
        route = respx.post(EMBED_URL)
        assert await fw.embed_text([]) == []
        assert route.call_count == 0


@pytest.mark.anyio
@respx.mock
async def test_embed_failure_names_the_failing_batch(monkeypatch):
    monkeypatch.setattr(settings, "embed_batch_size", 2)
    respx.post(EMBED_URL).mock(return_value=httpx.Response(429, text="rate limited"))

    with pytest.raises(fw.FireworksError) as exc:
        await fw.embed_text(["a", "b", "c"])

    # Actionable on a long ingestion run: which items, not just "it failed".
    assert "0-1" in str(exc.value)
    assert "429" in str(exc.value)


@pytest.mark.anyio
@respx.mock
async def test_chat_completion_returns_the_reply_text():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "the answer"}}]}
        )
    )

    assert await fw.chat_completion([{"role": "user", "content": "q"}]) == "the answer"


@pytest.mark.anyio
@respx.mock
async def test_chat_completion_sends_low_temperature_by_default():
    import json

    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}]}
        )
    )

    await fw.chat_completion([{"role": "user", "content": "q"}])

    # Grounded answers, not creative ones.
    assert json.loads(route.calls.last.request.content)["temperature"] == 0.2


@pytest.mark.anyio
@respx.mock
async def test_chat_failure_raises_fireworks_error():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(fw.FireworksError, match="500"):
        await fw.chat_completion([{"role": "user", "content": "q"}])
