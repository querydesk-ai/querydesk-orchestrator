"""
Thin wrapper around the Fireworks API. Two things only: chat completion
(for RAG answer generation) and embeddings (for retrieval + category
matching). Nothing else in the app should call httpx against Fireworks
directly — go through here so there's one place to change if the API
or model names change.
"""

import httpx

from querydesk.core.config import settings
from querydesk.core.errors import LLMError

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"

# qwen3-embedding-8b is asymmetric: documents are embedded as-is, while queries
# are wrapped in an instruction describing the retrieval task. Measured on a
# 4-article / 4-query set, this widens the mean margin between the correct
# article and the runner-up from +0.233 (no instruction) to +0.382. Accuracy
# was 4/4 either way — the margin is what matters, because the decision layer
# thresholds on this score, so better separation means a calibratable signal.
QUERY_INSTRUCTION = (
    "Instruct: Given a user's service desk question, retrieve the knowledge "
    "base article that answers it\nQuery: "
)


class FireworksError(LLMError):
    """Raised on any non-2xx response from Fireworks, or missing config."""


def _auth_headers() -> dict:
    if not settings.fireworks_api_key:
        raise FireworksError(
            "FIREWORKS_API_KEY is not set — copy .env.example to .env and fill it in."
        )
    return {"Authorization": f"Bearer {settings.fireworks_api_key}"}


async def chat_completion(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """
    Send a chat completion request. Returns the assistant's reply text.
    Used for RAG answer generation — keep temperature low since we want
    grounded, consistent answers, not creative ones.
    """
    payload = {
        "model": model or settings.fireworks_chat_model,
        "messages": messages,
        "max_tokens": settings.chat_max_tokens if max_tokens is None else max_tokens,
        "temperature": settings.chat_temperature if temperature is None else temperature,
    }
    headers = _auth_headers()

    async with httpx.AsyncClient(timeout=settings.chat_timeout_s) as client:
        resp = await client.post(
            f"{FIREWORKS_BASE_URL}/chat/completions", json=payload, headers=headers
        )
    if resp.status_code != 200:
        raise FireworksError(f"Fireworks chat call failed: {resp.status_code} {resp.text}")

    try:
        choice = resp.json()["choices"][0]
        content = (choice["message"].get("content") or "").strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        # A malformed 200 must still surface as a FireworksError (→ 502), not a 500.
        raise FireworksError(f"Fireworks chat response was malformed: {resp.text[:200]}") from e

    if not content:
        # The models on this account are reasoning models: hidden reasoning
        # tokens come out of the same budget as the answer, so a tight
        # max_tokens can be spent entirely on reasoning and return a 200 with
        # empty content. Surfacing that as an error beats handing the user a
        # blank reply and calling it an answer.
        reasoning = choice["message"].get("reasoning_content") or ""
        raise FireworksError(
            "Fireworks returned an empty answer "
            f"(finish_reason={choice.get('finish_reason')!r}, "
            f"max_tokens={payload['max_tokens']}, "
            f"reasoning_chars={len(reasoning)}). "
            "The model likely spent the whole token budget on reasoning — "
            "raise chat_max_tokens."
        )

    return content


async def embed_text(texts: list[str], model: str | None = None) -> list[list[float]]:
    """
    Embed one or more strings exactly as given. Returns one vector per input
    string, same order, batched at settings.embed_batch_size so a large
    ingestion run doesn't send one oversized request.

    Prefer embed_documents()/embed_query() — they apply the instruction format
    the model expects. Call this directly only for already-formatted text.
    """
    if not texts:
        return []

    headers = _auth_headers()
    batch_size = settings.embed_batch_size
    vectors: list[list[float]] = []

    async with httpx.AsyncClient(timeout=settings.embed_timeout_s) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            payload = {
                "model": model or settings.fireworks_embed_model,
                "input": batch,
            }
            resp = await client.post(
                f"{FIREWORKS_BASE_URL}/embeddings", json=payload, headers=headers
            )
            if resp.status_code != 200:
                # Name the batch: on a 500-chunk ingestion run, "it failed" is
                # not actionable but "items 64-95 failed" is.
                raise FireworksError(
                    f"Fireworks embed call failed for items "
                    f"{start}-{start + len(batch) - 1}: {resp.status_code} {resp.text}"
                )
            try:
                # Fireworks returns results in the same order as input.
                batch_vectors = [item["embedding"] for item in resp.json()["data"]]
            except (ValueError, KeyError, TypeError) as e:
                raise FireworksError(
                    f"Fireworks embed response was malformed: {resp.text[:200]}"
                ) from e
            if len(batch_vectors) != len(batch):
                raise FireworksError(
                    f"Fireworks returned {len(batch_vectors)} embeddings for "
                    f"{len(batch)} inputs (items {start}-{start + len(batch) - 1})"
                )
            vectors.extend(batch_vectors)

    return vectors


async def embed_documents(texts: list[str], model: str | None = None) -> list[list[float]]:
    """
    Embed knowledge-base chunks or category descriptions for storage.

    Documents get no instruction prefix — only queries do. Must pair with
    embed_query() on the read side.
    """
    return await embed_text(texts, model=model)


async def embed_query(text: str, model: str | None = None) -> list[float]:
    """
    Embed a user query for search, wrapped in the retrieval instruction.
    Must pair with embed_documents() — dropping the instruction costs
    threshold separation (see QUERY_INSTRUCTION).
    """
    vectors = await embed_text([QUERY_INSTRUCTION + text], model=model)
    return vectors[0]
