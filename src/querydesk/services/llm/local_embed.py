"""
Local dense embeddings via fastembed (BAAI/bge-base-en-v1.5 by default), on CPU.

The alternative to Fireworks' qwen3 for the dense half of retrieval and the
router: ~40 ms per query instead of ~2 s over the network (Day 5 spike,
docs/latency-recommendation.md). Selected by `embed_backend = "local"`; reach
it through services/retrieval/embedding.py, not directly.

bge is asymmetric like qwen3, with its own convention: queries carry
LOCAL_QUERY_PREFIX, documents are embedded as-is.

fastembed is synchronous and CPU-bound, so async callers use the async
wrappers, which run it in a worker thread.
"""

import anyio

from querydesk.core.config import settings
from querydesk.core.errors import LocalEmbedError

LOCAL_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model = None


def _get_model():
    """Lazily load and cache the model — loading downloads weights on first use."""
    global _model
    if _model is None:
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(model_name=settings.local_embed_model)
        except Exception as e:
            raise LocalEmbedError(
                f"Failed to load local embedding model {settings.local_embed_model!r}: {e}"
            ) from e
    return _model


def _embed(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    try:
        return [v.tolist() for v in model.embed(texts, batch_size=settings.embed_batch_size)]
    except Exception as e:
        raise LocalEmbedError(f"Local embedding failed: {e}") from e


def embed_documents_sync(texts: list[str]) -> list[list[float]]:
    """Embed KB chunks or category descriptions as-is. One vector per input, in order."""
    if not texts:
        return []
    return _embed(texts)


def embed_query_sync(text: str) -> list[float]:
    """Embed a user query for search, with bge's query prefix."""
    return _embed([LOCAL_QUERY_PREFIX + text])[0]


async def embed_documents(texts: list[str]) -> list[list[float]]:
    return await anyio.to_thread.run_sync(embed_documents_sync, texts)


async def embed_query(text: str) -> list[float]:
    return await anyio.to_thread.run_sync(embed_query_sync, text)
