"""App start: Qdrant collections and the BM25 warm-up, neither of which may stop the app."""

import pytest

from querydesk import main


@pytest.fixture
def startup(monkeypatch):
    calls = []

    async def ensure_collections():
        calls.append("ensure_collections")

    async def warm_up():
        calls.append("bm25")

    async def aclose():
        pass

    monkeypatch.setattr(main, "ensure_collections", ensure_collections)
    monkeypatch.setattr(main.sparse, "warm_up", warm_up)
    for mod in (main.ticketing, main.redis_store, main.postgres_store):
        monkeypatch.setattr(mod, "aclose", aclose)
    return calls


@pytest.mark.anyio
async def test_start_loads_the_bm25_model(startup):
    async with main.lifespan(main.app):
        assert startup == ["ensure_collections", "bm25"]


@pytest.mark.anyio
async def test_a_failed_warm_up_does_not_stop_the_app(startup, monkeypatch):
    async def broken():
        raise OSError("no network to download the model")

    monkeypatch.setattr(main.sparse, "warm_up", broken)

    async with main.lifespan(main.app):
        pass
