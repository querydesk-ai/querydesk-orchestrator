"""
The only module that talks to Redis. Nothing else may open a Redis connection.

It backs the query-embedding cache (bytes) and slot-filling state
(conversation:{user_id}, JSON), and drops duplicate WhatsApp deliveries
(wa:msg:{MessageSid}). Every failure — including a
missing REDIS_URL — raises RedisStoreError, so callers decide whether Redis
is optional (the cache) or required (conversation state).
"""

import json

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


async def get_json(key: str):
    """The JSON value at `key`, or None if it is absent."""
    raw = await get_bytes(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise RedisStoreError(f"Redis value at {key!r} is not JSON: {e}") from e


async def set_json(key: str, value, ttl_s: int) -> None:
    await set_bytes(key, json.dumps(value).encode("utf-8"), ttl_s)


async def set_if_absent(key: str, ttl_s: int) -> bool:
    """Claim `key` for `ttl_s`. True if it was free, False if already claimed (a duplicate)."""
    try:
        return bool(await _get_client().set(key, b"1", ex=ttl_s, nx=True))
    except RedisError as e:
        raise RedisStoreError(f"Redis SET NX failed: {e}") from e


async def delete(key: str) -> None:
    try:
        await _get_client().delete(key)
    except RedisError as e:
        raise RedisStoreError(f"Redis DEL failed: {e}") from e
