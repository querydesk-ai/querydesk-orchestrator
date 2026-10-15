"""
Query-embedding cache. Redis is fakeredis, Fireworks and BM25 are stubbed, so
these run offline and assert on what reached Fireworks.
"""

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from querydesk.core.config import settings
from querydesk.services.retrieval import query
from querydesk.services.state import redis_store


@pytest.fixture
def fireworks_calls(monkeypatch):
    """Stub the dense embed (records each call) and the CPU BM25 embed."""
    calls = []

    async def fake_embed_query(text):
        calls.append(text)
        return [0.25, -0.5, 1.0]

    async def fake_sparse(text):
        return object()

    monkeypatch.setattr(query, "embed_query", fake_embed_query)
    monkeypatch.setattr(query.sparse, "sparse_embed_query_async", fake_sparse)
    return calls


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(settings, "query_cache_enabled", True)
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis_store, "_client", client)
    return client


@pytest.mark.anyio
async def test_a_repeated_query_skips_fireworks(fireworks_calls, fake_redis):
    first = await query.embed_query_vectors("restart the claims app")
    second = await query.embed_query_vectors("restart the claims app")

    assert fireworks_calls == ["restart the claims app"]
    assert second.dense == first.dense == [0.25, -0.5, 1.0]


@pytest.mark.anyio
async def test_a_miss_writes_the_vector_with_a_ttl(fireworks_calls, fake_redis):
    await query.embed_query_vectors("restart the claims app")

    key = query.cache_key("restart the claims app")
    assert await fake_redis.get(key) is not None
    assert 0 < await fake_redis.ttl(key) <= settings.query_cache_ttl_s


@pytest.mark.anyio
async def test_whitespace_differences_share_an_entry(fireworks_calls, fake_redis):
    await query.embed_query_vectors("restart  the claims app ")
    await query.embed_query_vectors("restart the claims app")

    assert len(fireworks_calls) == 1


def test_changing_the_model_changes_the_key(monkeypatch):
    before = query.cache_key("q")
    monkeypatch.setattr(settings, "fireworks_embed_model", "another-model")

    assert query.cache_key("q") != before


@pytest.mark.anyio
async def test_a_redis_outage_falls_back_to_fireworks(fireworks_calls, fake_redis, monkeypatch):
    async def broken(*args, **kwargs):
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "get", broken)
    monkeypatch.setattr(fake_redis, "set", broken)

    vectors = await query.embed_query_vectors("restart the claims app")

    assert vectors.dense == [0.25, -0.5, 1.0]
    assert fireworks_calls == ["restart the claims app"]


@pytest.mark.anyio
async def test_no_redis_url_means_no_cache(fireworks_calls, monkeypatch):
    monkeypatch.setattr(settings, "query_cache_enabled", True)
    monkeypatch.setattr(settings, "redis_url", "")

    await query.embed_query_vectors("q")
    await query.embed_query_vectors("q")

    assert len(fireworks_calls) == 2
