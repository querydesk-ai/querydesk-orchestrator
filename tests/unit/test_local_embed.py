"""
Local fastembed backend. TextEmbedding is replaced by a fake, so nothing is
downloaded and these run offline.
"""

import numpy as np
import pytest

from querydesk.core.errors import LocalEmbedError
from querydesk.services.llm import local_embed


class FakeModel:
    def __init__(self):
        self.seen: list[str] = []

    def embed(self, texts, batch_size=None):
        for t in texts:
            self.seen.append(t)
            yield np.array([float(len(t)), 1.0])


@pytest.fixture
def fake_model(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(local_embed, "_model", model)
    return model


def test_queries_get_the_bge_prefix(fake_model):
    local_embed.embed_query_sync("reset my password")

    assert fake_model.seen == [local_embed.LOCAL_QUERY_PREFIX + "reset my password"]


def test_documents_are_embedded_as_is_in_order(fake_model):
    vectors = local_embed.embed_documents_sync(["a", "bbb", "cc"])

    assert fake_model.seen == ["a", "bbb", "cc"]
    assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]
    assert all(isinstance(x, float) for v in vectors for x in v)


def test_no_documents_skips_the_model(fake_model):
    assert local_embed.embed_documents_sync([]) == []
    assert fake_model.seen == []


def test_an_embed_failure_is_a_local_embed_error(monkeypatch):
    class Broken:
        def embed(self, texts, batch_size=None):
            raise RuntimeError("onnx exploded")

    monkeypatch.setattr(local_embed, "_model", Broken())

    with pytest.raises(LocalEmbedError):
        local_embed.embed_query_sync("q")


def test_a_load_failure_is_a_local_embed_error(monkeypatch):
    import fastembed

    def broken(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(local_embed, "_model", None)
    monkeypatch.setattr(fastembed, "TextEmbedding", broken)

    with pytest.raises(LocalEmbedError):
        local_embed.embed_query_sync("q")


@pytest.mark.anyio
async def test_async_wrappers_return_the_same_vectors(fake_model):
    assert await local_embed.embed_query("q") == local_embed.embed_query_sync("q")
    assert await local_embed.embed_documents(["x"]) == local_embed.embed_documents_sync(["x"])
