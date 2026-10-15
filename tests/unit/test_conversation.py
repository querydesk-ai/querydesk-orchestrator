"""
Slot-filling state on fakeredis: round trip, TTL, and that a Redis failure
degrades to "no conversation" instead of raising.
"""

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from querydesk.core.config import settings
from querydesk.services import conversation
from querydesk.services.conversation import Conversation
from querydesk.services.state import redis_store


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis_store, "_client", client)
    return client


def _conv(**overrides):
    return Conversation(
        **{
            "user_id": "u1",
            "category_id": "it_uat_extension",
            "original_query": "keep UAT-2 up",
            "fields": {"environment": "UAT-2"},
            "missing": ["extend_until", "reason"],
            "retrieval_confidence": 0.61,
            **overrides,
        }
    )


@pytest.mark.anyio
async def test_round_trip_and_ttl(fake_redis):
    await conversation.save(_conv(turns=["till 11 PM"], follow_ups=1))

    loaded = await conversation.load("u1")

    assert loaded == _conv(turns=["till 11 PM"], follow_ups=1)
    ttl = await fake_redis.ttl("conversation:u1")
    assert 0 < ttl <= settings.conversation_ttl_s


@pytest.mark.anyio
async def test_clear_removes_it_and_other_users_are_separate(fake_redis):
    await conversation.save(_conv())
    await conversation.save(_conv(user_id="u2"))

    await conversation.clear("u1")

    assert await conversation.load("u1") is None
    assert (await conversation.load("u2")).user_id == "u2"


def test_transcript_joins_the_whole_exchange():
    conv = _conv(turns=["UAT-2"])
    assert conv.transcript("till 11 PM") == "keep UAT-2 up\nUAT-2\ntill 11 PM"


class _DownRedis:
    async def get(self, *args, **kwargs):
        raise RedisConnectionError("down")

    set = delete = get


@pytest.mark.anyio
async def test_redis_down_degrades_without_raising(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    monkeypatch.setattr(redis_store, "_client", _DownRedis())

    assert await conversation.load("u1") is None
    await conversation.save(_conv())  # no exception
    await conversation.clear("u1")


@pytest.mark.anyio
async def test_unconfigured_redis_means_no_conversation():
    # conftest leaves REDIS_URL empty.
    assert await conversation.load("u1") is None
    await conversation.save(_conv())


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [b"not json", b'{"unexpected": 1}'])
async def test_malformed_state_is_discarded(fake_redis, raw):
    await fake_redis.set("conversation:u1", raw)
    assert await conversation.load("u1") is None
