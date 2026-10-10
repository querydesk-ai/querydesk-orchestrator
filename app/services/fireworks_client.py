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
    Embed one or more strings. Returns one vector per input string, same
    order. Used for both knowledge-base chunk embedding and category
    embedding — same model for both so similarity scores stay comparable.
    """
    payload = {
        "model": model or settings.fireworks_embed_model,
        "input": texts,
    }
    headers = _auth_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{FIREWORKS_BASE_URL}/embeddings", json=payload, headers=headers
        )
    if resp.status_code != 200:
        raise FireworksError(f"Fireworks embed call failed: {resp.status_code} {resp.text}")

    data = resp.json()
    # Fireworks returns results in the same order as input
    return [item["embedding"] for item in data["data"]]
