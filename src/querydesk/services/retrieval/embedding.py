"""
The single entry point for dense embeddings, whichever backend is configured.

`settings.embed_backend` picks Fireworks (qwen3-embedding-8b, network) or the
local fastembed model (bge-base, CPU). Each backend applies its own
query/document asymmetry, so callers only choose documents vs query. The two
backends' vectors live in different spaces and sizes: a collection built with
one can't be searched with the other (ensure_collections() checks the size).
"""

from querydesk.core.config import settings
from querydesk.services.llm import fireworks, local_embed

BACKENDS = ("fireworks", "local")


def _backend():
    if settings.embed_backend == "fireworks":
        return fireworks
    if settings.embed_backend == "local":
        return local_embed
    raise ValueError(
        f"Unknown embed_backend {settings.embed_backend!r}; expected one of {BACKENDS}"
    )


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed KB chunks or category descriptions for storage."""
    return await _backend().embed_documents(texts)


async def embed_query(text: str) -> list[float]:
    """Embed a user query for search or routing."""
    return await _backend().embed_query(text)
