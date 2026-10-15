"""
Slot-filling state: one open conversation per user, conversation:{user_id}
in Redis with a TTL (PRD F4, docs/plan-day-6.md).

A conversation exists only while an SR is waiting for required fields. It is
a convenience, not a record: if Redis is down or unconfigured, every function
here degrades (load -> None, save/clear -> no-op) with a warning, so /chat
falls back to single-turn behaviour instead of failing.
"""

import logging
from dataclasses import asdict, dataclass, field

from querydesk.core.config import settings
from querydesk.core.errors import RedisStoreError
from querydesk.services.state import redis_store

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    user_id: str
    category_id: str
    original_query: str
    fields: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    # The user's replies so far, so re-extraction sees the whole exchange.
    turns: list[str] = field(default_factory=list)
    follow_ups: int = 0
    retrieval_confidence: float | None = None

    def transcript(self, latest: str) -> str:
        return "\n".join([self.original_query, *self.turns, latest])


def _key(user_id: str) -> str:
    return f"conversation:{user_id}"


async def load(user_id: str) -> Conversation | None:
    try:
        data = await redis_store.get_json(_key(user_id))
    except RedisStoreError as e:
        logger.warning("Conversation state unavailable, treating as a new message: %s", e)
        return None
    if data is None:
        return None
    try:
        return Conversation(**data)
    except TypeError:
        logger.warning("Discarding malformed conversation state for %s", user_id)
        return None


async def save(conv: Conversation) -> None:
    try:
        await redis_store.set_json(_key(conv.user_id), asdict(conv), settings.conversation_ttl_s)
    except RedisStoreError as e:
        logger.warning("Could not save conversation state: %s", e)


async def clear(user_id: str) -> None:
    try:
        await redis_store.delete(_key(user_id))
    except RedisStoreError as e:
        logger.warning("Could not clear conversation state: %s", e)
