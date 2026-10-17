"""The embed_backend switch dispatches to exactly one backend."""

import pytest

from querydesk.core.config import settings
from querydesk.services.llm import fireworks, local_embed
from querydesk.services.retrieval import embedding


@pytest.fixture
def backends(monkeypatch):
    calls = []

    def stub(name):
        async def docs(texts):
            calls.append((name, "documents", texts))
            return [[0.0]] * len(texts)

        async def query(text):
            calls.append((name, "query", text))
            return [0.0]

        return docs, query

    fw_docs, fw_query = stub("fireworks")
    lo_docs, lo_query = stub("local")
    monkeypatch.setattr(fireworks, "embed_documents", fw_docs)
    monkeypatch.setattr(fireworks, "embed_query", fw_query)
    monkeypatch.setattr(local_embed, "embed_documents", lo_docs)
    monkeypatch.setattr(local_embed, "embed_query", lo_query)
    return calls


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["fireworks", "local"])
async def test_dispatches_to_the_configured_backend(backends, monkeypatch, backend):
    monkeypatch.setattr(settings, "embed_backend", backend)

    await embedding.embed_query("q")
    await embedding.embed_documents(["d"])

    assert backends == [(backend, "query", "q"), (backend, "documents", ["d"])]


@pytest.mark.anyio
async def test_an_unknown_backend_fails_loudly(backends, monkeypatch):
    monkeypatch.setattr(settings, "embed_backend", "openai")

    with pytest.raises(ValueError, match="openai"):
        await embedding.embed_query("q")
    assert backends == []


def test_the_model_id_follows_the_backend(monkeypatch):
    monkeypatch.setattr(settings, "embed_backend", "fireworks")
    assert settings.embed_model_id == settings.fireworks_embed_model

    monkeypatch.setattr(settings, "embed_backend", "local")
    assert settings.embed_model_id == settings.local_embed_model
