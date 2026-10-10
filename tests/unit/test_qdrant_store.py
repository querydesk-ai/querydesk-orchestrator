"""
ensure_collections() dimension guard. A fake client stands in for Qdrant, so
no network is involved.
"""
from types import SimpleNamespace

import pytest

from querydesk.core.config import settings
from querydesk.services.retrieval import qdrant_store


class FakeClient:
    def __init__(self, dense_size: int):
        self.dense_size = dense_size
        self.created: list[str] = []

    async def collection_exists(self, name):
        return True

    async def get_collection(self, name):
        vectors = {qdrant_store.DENSE_VECTOR: SimpleNamespace(size=self.dense_size)}
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))

    async def create_collection(self, collection_name, **kwargs):
        self.created.append(collection_name)


@pytest.mark.anyio
async def test_existing_collection_with_wrong_dim_fails_at_boot(monkeypatch):
    """A collection built for a previous embedding model must not be reused silently."""
    monkeypatch.setattr(qdrant_store, "get_client", lambda: FakeClient(dense_size=768))
    monkeypatch.setattr(settings, "embed_dim", 4096)

    with pytest.raises(qdrant_store.QdrantStoreError, match="--recreate"):
        await qdrant_store.ensure_collections()


@pytest.mark.anyio
async def test_existing_collection_with_matching_dim_is_left_alone(monkeypatch):
    client = FakeClient(dense_size=4096)
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    monkeypatch.setattr(settings, "embed_dim", 4096)

    await qdrant_store.ensure_collections()

    assert client.created == []
