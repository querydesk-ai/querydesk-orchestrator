"""
The only module that talks to Qdrant.

Two collections:
  kb_chunks  — knowledge-base chunks, named vectors "dense" (cosine) + "bm25" (sparse)
  categories — the 10 categories from categories.json, dense only, used by the
               semantic router from PRD Day 3

Everything is async: the /chat path is async and the sync client would block
the event loop while waiting on network I/O.
"""
import uuid
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from querydesk.core.config import settings
from querydesk.core.errors import VectorStoreError

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"

# Namespace for deterministic point ids, so re-ingesting a document updates its
# chunks in place instead of appending duplicates.
_POINT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


class QdrantStoreError(VectorStoreError):
    """Raised on missing config or any Qdrant failure."""


@dataclass
class RetrievedChunk:
    text: str
    title: str
    source_path: str
    source_url: str | None
    category_id: str | None
    chunk_index: int
    score: float


_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient:
    """Lazily created shared client, following the `settings` singleton precedent."""
    global _client
    if _client is None:
        if not settings.qdrant_url:
            raise QdrantStoreError(
                "QDRANT_URL is not set — copy .env.example to .env and fill it in."
            )
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=30,
        )
    return _client


def point_id(source_path: str, chunk_index: int) -> str:
    """Stable id for a chunk, so re-ingestion overwrites rather than duplicates."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}:{chunk_index}"))


async def ensure_collections(recreate: bool = False) -> None:
    """
    Create both collections if absent. Idempotent, so it's safe to call on
    every app start — which is the point: bad Qdrant config should fail at boot
    rather than on a user's first message.

    An existing collection whose dense size differs from settings.embed_dim
    (e.g. built for a previous embedding model) raises rather than being
    reused — otherwise every query would fail later with an opaque 502.
    """
    client = get_client()
    try:
        if not recreate:
            await _check_dense_dims(client)

        if recreate:
            for name in (
                settings.qdrant_kb_collection,
                settings.qdrant_categories_collection,
            ):
                if await client.collection_exists(name):
                    await client.delete_collection(name)

        if not await client.collection_exists(settings.qdrant_kb_collection):
            await client.create_collection(
                collection_name=settings.qdrant_kb_collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=settings.embed_dim, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    # The IDF modifier is what makes Qdrant apply BM25's
                    # inverse-document-frequency term weighting. Without it
                    # search still "works" but scores are meaningfully worse,
                    # and the failure is entirely silent.
                    SPARSE_VECTOR: models.SparseVectorParams(
                        modifier=models.Modifier.IDF
                    )
                },
            )

        if not await client.collection_exists(settings.qdrant_categories_collection):
            await client.create_collection(
                collection_name=settings.qdrant_categories_collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=settings.embed_dim, distance=models.Distance.COSINE
                    )
                },
            )
    except QdrantStoreError:
        raise
    except Exception as e:
        raise QdrantStoreError(f"Failed to ensure Qdrant collections: {e}") from e


async def _check_dense_dims(client: AsyncQdrantClient) -> None:
    for name in (settings.qdrant_kb_collection, settings.qdrant_categories_collection):
        if not await client.collection_exists(name):
            continue
        info = await client.get_collection(name)
        vectors = info.config.params.vectors
        dense = vectors.get(DENSE_VECTOR) if isinstance(vectors, dict) else None
        if dense is None or dense.size != settings.embed_dim:
            found = dense.size if dense else "no 'dense' vector"
            raise QdrantStoreError(
                f"Collection {name!r} has dense size {found}, but embed_dim is "
                f"{settings.embed_dim}. Re-index with `python scripts/ingest.py --recreate`."
            )


async def ping() -> bool:
    """Liveness check for /health/ready."""
    try:
        await get_client().get_collections()
        return True
    except Exception as e:
        raise QdrantStoreError(f"Qdrant unreachable: {e}") from e


async def upsert_kb_chunks(points: list[models.PointStruct]) -> None:
    try:
        await get_client().upsert(
            collection_name=settings.qdrant_kb_collection, points=points, wait=True
        )
    except Exception as e:
        raise QdrantStoreError(f"Failed to upsert KB chunks: {e}") from e


async def upsert_categories(points: list[models.PointStruct]) -> None:
    try:
        await get_client().upsert(
            collection_name=settings.qdrant_categories_collection,
            points=points,
            wait=True,
        )
    except Exception as e:
        raise QdrantStoreError(f"Failed to upsert categories: {e}") from e


async def hybrid_search(
    dense_vector: list[float],
    sparse_vector: models.SparseVector,
    top_k: int | None = None,
) -> tuple[list[RetrievedChunk], float]:
    """
    Hybrid retrieval over kb_chunks. Returns (fused_hits, top_dense_score).

    Why two values: Qdrant's RRF fusion scores are derived from *ranks*, not
    similarities — a fused score of 0.5 says "roughly mid-table", not "50%
    similar" — so thresholding a fused score is meaningless (PRD Gap #2). The
    fused hits decide *what* the LLM sees; the separate top dense cosine
    similarity is the calibration-grade confidence signal the decision layer
    thresholds against from Day 3 onward.

    Both queries go out in one query_batch_points round trip.
    """
    limit = top_k or settings.retrieval_top_k
    prefetch_limit = settings.retrieval_prefetch_limit

    fused_request = models.QueryRequest(
        prefetch=[
            models.Prefetch(
                query=dense_vector, using=DENSE_VECTOR, limit=prefetch_limit
            ),
            models.Prefetch(
                query=sparse_vector, using=SPARSE_VECTOR, limit=prefetch_limit
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    # Dense-only, top 1: a real cosine similarity, untouched by fusion.
    dense_request = models.QueryRequest(
        query=dense_vector,
        using=DENSE_VECTOR,
        limit=1,
        with_payload=False,
    )

    try:
        responses = await get_client().query_batch_points(
            collection_name=settings.qdrant_kb_collection,
            requests=[fused_request, dense_request],
        )
    except Exception as e:
        raise QdrantStoreError(f"Hybrid search failed: {e}") from e

    fused_points = responses[0].points
    dense_points = responses[1].points

    hits = [
        RetrievedChunk(
            text=(p.payload or {}).get("text", ""),
            title=(p.payload or {}).get("title", ""),
            source_path=(p.payload or {}).get("source_path", ""),
            source_url=(p.payload or {}).get("source_url"),
            category_id=(p.payload or {}).get("category_id"),
            chunk_index=(p.payload or {}).get("chunk_index", 0),
            score=p.score,
        )
        for p in fused_points
    ]
    top_dense_score = dense_points[0].score if dense_points else 0.0

    return hits, top_dense_score
