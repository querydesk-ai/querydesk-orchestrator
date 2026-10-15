"""
The only module that talks to Redis. Nothing else may open a Redis connection.

Today it backs the query-embedding cache; Day 6 slot-filling state
(conversation:{user_id}) goes through here too. Every failure — including a
missing REDIS_URL — raises RedisStoreError, so callers decide whether Redis
is optional (the cache) or required (conversation state).
"""

import redis.asyncio as redis
from redis.exceptions import RedisError

from querydesk.core.config import settings
from querydesk.core.errors import RedisStoreError

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if not settings.redis_url:
        raise RedisStoreError("REDIS_URL is not set")
    if _client is None:
        _client = redis.from_url(settings.redis_url)
    return _client


async def aclose() -> None:
    """Close the shared client (app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get_bytes(key: str) -> bytes | None:
    try:
        return await _get_client().get(key)
    except RedisError as e:
        raise RedisStoreError(f"Redis GET failed: {e}") from e


async def set_bytes(key: str, value: bytes, ttl_s: int) -> None:
    try:
        await _get_client().set(key, value, ex=ttl_s)
    except RedisError as e:
        raise RedisStoreError(f"Redis SET failed: {e}") from e
