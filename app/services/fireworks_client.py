"""
Thin wrapper around the Fireworks API. Two things only: chat completion
(for RAG answer generation) and embeddings (for retrieval + category
matching). Nothing else in the app should call httpx against Fireworks
directly — go through here so there's one place to change if the API
or model names change.
"""
import httpx

from app.config import settings

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"

# nomic-embed-text-v1.5 is an asymmetric, instruction-prefixed model: it was
# trained with a task prefix on every input, and retrieval quality drops
# measurably without them. Documents and queries get *different* prefixes, so
# callers should use embed_documents()/embed_query() and never pass raw text.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class FireworksError(RuntimeError):
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
    max_tokens: int = 512,
    temperature: float = 0.2,
) -> str:
    """
    Send a chat completion request. Returns the assistant's reply text.
    Used for RAG answer generation — keep temperature low since we want
    grounded, consistent answers, not creative ones.
    """
    payload = {
        "model": model or settings.fireworks_chat_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = _auth_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{FIREWORKS_BASE_URL}/chat/completions", json=payload, headers=headers
        )
    if resp.status_code != 200:
        raise FireworksError(f"Fireworks chat call failed: {resp.status_code} {resp.text}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def embed_text(texts: list[str], model: str | None = None) -> list[list[float]]:
    """
    Embed one or more strings exactly as given. Returns one vector per input
    string, same order, batched at settings.embed_batch_size so a large
    ingestion run doesn't send one oversized request.

    Prefer embed_documents()/embed_query() — they apply the task prefix the
    model expects. Call this directly only when the text is already prefixed.
    """
    if not texts:
        return []

    headers = _auth_headers()
    batch_size = settings.embed_batch_size
    vectors: list[list[float]] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
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
            data = resp.json()
            # Fireworks returns results in the same order as input.
            vectors.extend(item["embedding"] for item in data["data"])

    return vectors


async def embed_documents(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed knowledge-base chunks or category descriptions for storage."""
    return await embed_text([DOCUMENT_PREFIX + t for t in texts], model=model)


async def embed_query(text: str, model: str | None = None) -> list[float]:
    """
    Embed a user query for search. Must pair with embed_documents() — mixing
    prefixes across the two sides of a comparison degrades the similarity.
    """
    vectors = await embed_text([QUERY_PREFIX + text], model=model)
    return vectors[0]
