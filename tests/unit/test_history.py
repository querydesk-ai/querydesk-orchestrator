"""
Chat history on fakeredis: round trip, the cap, TTL, separation by user, and
that a Redis failure or a bad blob degrades to "no history" instead of raising.
"""

import asyncio

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from querydesk.core.config import settings
from querydesk.services import history, query_rewrite
from querydesk.services.llm.fireworks import FireworksError
from querydesk.services.state import redis_store


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis_store, "_client", client)
    return client


@pytest.mark.anyio
async def test_round_trip_oldest_first_with_ttl(fake_redis):
    await history.append("u1", "hello there", "Hi!", "SMALLTALK")
    await history.append("u1", "restart claims", "Here's what I'll raise", "CONFIRM_SR")

    turns = await history.load("u1")

    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "hello there"),
        ("bot", "Hi!"),
        ("user", "restart claims"),
        ("bot", "Here's what I'll raise"),
    ]
    assert turns[1]["decision"] == "SMALLTALK" and turns[0]["decision"] is None
    ttl = await fake_redis.ttl("history:u1")
    assert 0 < ttl <= settings.history_ttl_s


@pytest.mark.anyio
async def test_only_the_newest_exchanges_are_kept(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "history_turns", 2)
    for i in range(4):
        await history.append("u1", f"q{i}", f"a{i}")

    turns = await history.load("u1")

    assert [t["text"] for t in turns] == ["q2", "a2", "q3", "a3"]


@pytest.mark.anyio
async def test_users_are_separate_and_long_text_is_capped(fake_redis):
    await history.append("u1", "x" * 5000, "ok")
    await history.append("u2", "mine", "ok")

    assert len((await history.load("u1"))[0]["text"]) == 1000
    assert [t["text"] for t in await history.load("u2")] == ["mine", "ok"]
    assert await history.load("nobody") == []


@pytest.mark.anyio
async def test_a_malformed_blob_is_ignored(fake_redis):
    await fake_redis.set("history:u1", b'{"not": "a list"}')
    assert await history.load("u1") == []

    await fake_redis.set("history:u1", b'[{"role": "bot"}, "junk", null]')
    assert await history.load("u1") == []

    await fake_redis.set("history:u1", b"not json")
    assert await history.load("u1") == []


class _DownRedis:
    async def get(self, *args, **kwargs):
        raise RedisConnectionError("down")

    set = delete = get


@pytest.mark.anyio
async def test_redis_down_means_no_history_not_an_error(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    monkeypatch.setattr(redis_store, "_client", _DownRedis())

    assert await history.load("u1") == []
    await history.append("u1", "hi", "Hi!")  # no raise


@pytest.mark.anyio
async def test_no_redis_configured_means_no_history():
    assert await history.load("u1") == []
    await history.append("u1", "hi", "Hi!")


# --- The follow-up rewrite -----------------------------------------------------

TURNS = [
    {"role": "user", "text": "How long does a UAT extension take?", "decision": None, "at": "t"},
    {"role": "bot", "text": "Usually a day.", "decision": "ANSWER", "at": "t"},
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("and for prod?", True),
        ("what about the same for UAT-3", True),
        ("how long does it take?", True),
        ("how do I reset my password?", False),
        ("restart the claims app in UAT", False),
    ],
)
def test_only_messages_leaning_on_earlier_turns_are_candidates(text, expected):
    assert query_rewrite.has_reference(text) is expected


def _llm(monkeypatch, reply=None, error=None, delay=0.0):
    calls = []

    async def fake(messages, **kwargs):
        calls.append(messages[0]["content"])
        if delay:
            await asyncio.sleep(delay)
        if error:
            raise error
        return reply

    monkeypatch.setattr(query_rewrite, "chat_completion", fake)
    return calls


@pytest.mark.anyio
async def test_a_follow_up_is_rewritten_with_the_chat_as_context(monkeypatch):
    calls = _llm(monkeypatch, reply='  "How long does a UAT extension take for prod?"\n')

    out = await query_rewrite.standalone("and for prod?", TURNS)

    assert out == "How long does a UAT extension take for prod?"
    assert "User: How long does a UAT extension take?" in calls[0]
    assert "Latest message: and for prod?" in calls[0]


@pytest.mark.anyio
async def test_no_history_or_no_reference_means_no_llm_call(monkeypatch):
    calls = _llm(monkeypatch, reply="rewritten")

    assert await query_rewrite.standalone("and for prod?", []) == "and for prod?"
    assert await query_rewrite.standalone("reset my password", TURNS) == "reset my password"
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["", "   ", "x" * 2000])
async def test_an_empty_or_runaway_rewrite_is_discarded(monkeypatch, reply):
    _llm(monkeypatch, reply=reply)

    assert await query_rewrite.standalone("and for prod?", TURNS) == "and for prod?"


@pytest.mark.anyio
async def test_a_failed_or_slow_call_falls_back_to_the_original(monkeypatch):
    _llm(monkeypatch, error=FireworksError("down"))
    assert await query_rewrite.standalone("and for prod?", TURNS) == "and for prod?"

    monkeypatch.setattr(settings, "history_rewrite_timeout_s", 0.01)
    _llm(monkeypatch, reply="rewritten", delay=1.0)
    assert await query_rewrite.standalone("and for prod?", TURNS) == "and for prod?"
