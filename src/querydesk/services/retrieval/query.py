"""
Embed a user query once, both ways.

The dense vector feeds hybrid search *and* the category router, so it is
computed here once per message and passed around — embedding twice would cost
a second Fireworks round trip on the latency-sensitive /chat path.
"""

from dataclasses import dataclass

import anyio
from qdrant_client import models

from querydesk.services.llm.fireworks import embed_query
from querydesk.services.retrieval import sparse


@dataclass
class QueryVectors:
    dense: list[float]
    sparse: models.SparseVector


async def embed_query_vectors(query: str) -> QueryVectors:
    """Dense (Fireworks, network) and sparse (BM25, CPU) embeddings, overlapped."""
    holder: dict = {}

    async def _dense():
        holder["dense"] = await embed_query(query)

    async def _sparse():
        holder["sparse"] = await sparse.sparse_embed_query_async(query)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_dense)
        tg.start_soon(_sparse)

    return QueryVectors(dense=holder["dense"], sparse=holder["sparse"])
