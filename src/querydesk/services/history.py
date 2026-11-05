"""
Recent chat history: the last few exchanges per user, history:{user_id} in
Redis with a TTL.

Like conversation.py it is a convenience, not a record: if Redis is down or
unconfigured, load returns [] and append is a no-op with a warning, so /chat
never fails because of it. Only the user's text and the bot's reply are kept
(no sources, no phone numbers: the id is the app user id, `wa-…` on WhatsApp).
The blob is read-modify-written, which is fine for one person typing in turn.
"""

import logging
from datetime import UTC, datetime

from querydesk.core.config import settings
from querydesk.core.errors import RedisStoreError
from querydesk.services.state import redis_store

logger = logging.getLogger(__name__)

# One reply can be a whole KB answer; cap what is stored per message.
_MAX_TEXT_CHARS = 1000


def _key(user_id: str) -> str:
    return f"history:{user_id}"


def _valid(turn) -> bool:
    return (
        isinstance(turn, dict)
        and turn.get("role") in ("user", "bot")
        and isinstance(turn.get("text"), str)
        and isinstance(turn.get("at"), str)
    )


async def load(user_id: str) -> list[dict]:
    """Oldest first. [] when there is none, or the store is unavailable."""
    try:
        data = await redis_store.get_json(_key(user_id))
    except RedisStoreError as e:
        logger.warning("Chat history unavailable: %s", e)
        return []
    if not isinstance(data, list):
        if data is not None:
            logger.warning("Discarding malformed chat history for %s", user_id)
        return []
    return [t for t in data if _valid(t)]


async def append(user_id: str, user_text: str, reply: str, decision: str | None = None) -> None:
    """Add one exchange, keeping the newest `history_turns` of them."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    turns = await load(user_id)
    turns += [
        {"role": "user", "text": user_text[:_MAX_TEXT_CHARS], "decision": None, "at": now},
        {"role": "bot", "text": reply[:_MAX_TEXT_CHARS], "decision": decision, "at": now},
    ]
    turns = turns[-2 * settings.history_turns :]
    try:
        await redis_store.set_json(_key(user_id), turns, settings.history_ttl_s)
    except RedisStoreError as e:
        logger.warning("Could not save chat history: %s", e)
