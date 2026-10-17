"""
Embed a user query once, both ways.

The dense vector feeds hybrid search *and* the category router, so it is
computed here once per message and passed around — embedding twice would cost
a second Fireworks round trip on the latency-sensitive /chat path.

Dense vectors are also cached in Redis, keyed by model and normalised text, so
a repeated message skips Fireworks entirely. The cache is best-effort: any
Redis failure is a miss, never a failed request.
"""

import hashlib
import logging
from array import array
from dataclasses import dataclass

import anyio
from qdrant_client import models

from querydesk.core.config import settings
from querydesk.core.errors import RedisStoreError
from querydesk.services.retrieval import sparse
from querydesk.services.retrieval.embedding import embed_query
from querydesk.services.state import redis_store

logger = logging.getLogger(__name__)


@dataclass
class QueryVectors:
    dense: list[float]
    sparse: models.SparseVector


def cache_key(query: str) -> str:
    """The active model is in the key, so switching model or backend never serves stale vectors."""
    normalised = " ".join(query.split())
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return f"qemb:{settings.embed_model_id}:{digest}"


def _cache_enabled() -> bool:
    return settings.query_cache_enabled and bool(settings.redis_url)


async def _cached_dense(query: str) -> list[float]:
    if not _cache_enabled():
        return await embed_query(query)

    key = cache_key(query)
    try:
        raw = await redis_store.get_bytes(key)
    except RedisStoreError as e:
        logger.warning("query cache read failed, embedding instead: %s", e)
        return await embed_query(query)
    if raw is not None:
        return array("f", raw).tolist()

    dense = await embed_query(query)
    try:
        # float32 halves the payload (4096 dims ≈ 16 KB); cosine ranking is unaffected.
        await redis_store.set_bytes(key, array("f", dense).tobytes(), settings.query_cache_ttl_s)
    except RedisStoreError as e:
        logger.warning("query cache write failed: %s", e)
    return dense


async def embed_query_vectors(query: str) -> QueryVectors:
    """Dense (cache or the embed backend) and sparse (BM25, CPU) embeddings, overlapped."""
    holder: dict = {}

    async def _dense():
        holder["dense"] = await _cached_dense(query)

    async def _sparse():
        holder["sparse"] = await sparse.sparse_embed_query_async(query)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_dense)
        tg.start_soon(_sparse)

    return QueryVectors(dense=holder["dense"], sparse=holder["sparse"])
