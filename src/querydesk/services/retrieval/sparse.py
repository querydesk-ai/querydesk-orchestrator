"""
BM25 sparse vectors via fastembed, running on CPU.

This is the lexical half of hybrid retrieval, and it's there for the cases
dense embeddings are bad at: exact error codes, product names, hostnames and
version strings. A dense model will happily rank "ERR-5041" near "ERR-5014";
BM25 will not.

fastembed is synchronous and CPU-bound, so async callers must go through the
`*_async` helpers rather than calling the sync functions on the event loop.
"""

import anyio
from qdrant_client import models

SPARSE_MODEL_NAME = "Qdrant/bm25"

_model = None


def _get_model():
    """
    Lazily construct and cache the model. Construction downloads and
    initialises weights, so doing it per call would dominate ingestion time.
    Lazy rather than import-time so importing this module (in tests, or in a
    process that never embeds) stays cheap.
    """
    global _model
    if _model is None:
        from fastembed import SparseTextEmbedding

        _model = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)
    return _model


def _to_sparse_vector(embedding) -> models.SparseVector:
    return models.SparseVector(
        indices=embedding.indices.tolist(),
        values=embedding.values.tolist(),
    )


def sparse_embed_documents(texts: list[str]) -> list[models.SparseVector]:
    """Sparse-embed KB chunks for storage. Returns one vector per input, in order."""
    if not texts:
        return []
    return [_to_sparse_vector(e) for e in _get_model().embed(texts)]


def sparse_embed_query(text: str) -> models.SparseVector:
    """
    Sparse-embed a user query for search.

    Uses the model's dedicated query path: BM25 scores a query against the
    corpus statistics rather than treating it as another document, so this is
    not interchangeable with sparse_embed_documents().
    """
    return _to_sparse_vector(next(iter(_get_model().query_embed(text))))


async def sparse_embed_query_async(text: str) -> models.SparseVector:
    """Query-embed off the event loop — fastembed is sync and CPU-bound."""
    return await anyio.to_thread.run_sync(sparse_embed_query, text)


async def sparse_embed_documents_async(texts: list[str]) -> list[models.SparseVector]:
    """Document-embed off the event loop."""
    return await anyio.to_thread.run_sync(sparse_embed_documents, texts)
