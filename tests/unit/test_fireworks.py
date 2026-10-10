"""
Fireworks client tests. Fully offline — respx intercepts the HTTP calls, so
these run in CI with no API key.
"""
import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.services.llm import fireworks as fw

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
async def test_embed_documents_sends_text_unmodified():
    """
    qwen3-embedding-8b takes documents as-is; only queries get an instruction.
    A prefix leaking onto the document side would break the asymmetry the
    model was trained for.
    """
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    await fw.embed_documents(["how do I reset my password"])

    import json

    assert json.loads(route.calls.last.request.content)["input"] == [
        "how do I reset my password"
    ]


@pytest.mark.anyio
@respx.mock
async def test_embed_query_applies_the_retrieval_instruction():
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    await fw.embed_query("how do I reset my password")

    import json

    sent = json.loads(route.calls.last.request.content)["input"][0]
    assert sent == fw.QUERY_INSTRUCTION + "how do I reset my password"
    # Qwen3's documented shape: a task instruction, then the query.
    assert sent.startswith("Instruct: ")
    assert "\nQuery: " in sent


@pytest.mark.anyio
@respx.mock
async def test_only_the_query_side_is_instructed():
    """
    The asymmetry is the point: measured on a 4-article set, instructing the
    query widened the mean correct-vs-runner-up margin from +0.233 to +0.382.
    Instructing both sides, or neither, loses that separation.
    """
    route = respx.post(EMBED_URL).mock(side_effect=_embed_response)

    await fw.embed_documents(["some article text"])
    doc_input = __import__("json").loads(route.calls.last.request.content)["input"][0]

    await fw.embed_query("some article text")
    query_input = __import__("json").loads(route.calls.last.request.content)["input"][0]

    assert doc_input != query_input
    assert fw.QUERY_INSTRUCTION not in doc_input


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


@pytest.mark.anyio
@respx.mock
async def test_empty_content_from_a_reasoning_model_raises():
    """
    Every chat model on this account is a reasoning model: reasoning tokens
    come out of the same budget as the answer, so a tight max_tokens yields a
    200 with empty content and a full reasoning_content. Returning that as an
    answer would hand the user a blank reply; it has to be an error.
    """
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "Thinking about it at length...",
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        )
    )

    with pytest.raises(fw.FireworksError) as exc:
        await fw.chat_completion([{"role": "user", "content": "q"}])

    message = str(exc.value)
    # Diagnosable without re-running: says why, and what to change.
    assert "empty answer" in message
    assert "chat_max_tokens" in message
    assert "length" in message


@pytest.mark.anyio
@respx.mock
async def test_null_content_is_treated_as_empty():
    """A JSON null, not just an empty string — the API uses both."""
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}, "finish_reason": "length"}]},
        )
    )

    with pytest.raises(fw.FireworksError, match="empty answer"):
        await fw.chat_completion([{"role": "user", "content": "q"}])


@pytest.mark.anyio
@respx.mock
async def test_reply_is_stripped_of_surrounding_whitespace():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "  the answer\n\n"}}]}
        )
    )

    assert await fw.chat_completion([{"role": "user", "content": "q"}]) == "the answer"


@pytest.mark.anyio
@respx.mock
async def test_chat_uses_configured_token_budget(monkeypatch):
    import json

    monkeypatch.setattr(settings, "chat_max_tokens", 2048)
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}]}
        )
    )

    await fw.chat_completion([{"role": "user", "content": "q"}])

    assert json.loads(route.calls.last.request.content)["max_tokens"] == 2048


@pytest.mark.anyio
@respx.mock
async def test_explicit_max_tokens_is_not_replaced_by_default():
    """0 is a value, not "unset" — same `is None` rule as temperature."""
    import json

    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}]}
        )
    )

    await fw.chat_completion([{"role": "user", "content": "q"}], max_tokens=0)

    assert json.loads(route.calls.last.request.content)["max_tokens"] == 0


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, text="<html>gateway</html>"),
    ],
)
async def test_malformed_chat_response_raises_fireworks_error(response):
    """Must stay a FireworksError so the router maps it to 502, not a 500."""
    respx.post(CHAT_URL).mock(return_value=response)

    with pytest.raises(fw.FireworksError, match="malformed"):
        await fw.chat_completion([{"role": "user", "content": "q"}])


@pytest.mark.anyio
@respx.mock
async def test_malformed_embed_response_raises_fireworks_error():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json={"nope": []}))

    with pytest.raises(fw.FireworksError, match="malformed"):
        await fw.embed_documents(["a"])


@pytest.mark.anyio
@respx.mock
async def test_embed_count_mismatch_raises():
    """A short response would silently misalign vectors with their chunks."""
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    )

    with pytest.raises(fw.FireworksError, match="1 embeddings for 2 inputs"):
        await fw.embed_documents(["a", "b"])
