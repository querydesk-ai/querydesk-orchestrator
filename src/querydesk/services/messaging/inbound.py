"""
One WhatsApp message in, one reply out: the same pipeline as POST /chat,
under the user id derived from the sender's number.

Runs in the Celery worker (querydesk.whatsapp_message), never on the webhook
request. The user always gets an answer: a dependency failure becomes a
fixed "try again" reply, because nobody is watching a 502 on WhatsApp.
"""

import logging

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError, UpstreamError
from querydesk.services import pipeline
from querydesk.services.messaging import text, whatsapp
from querydesk.services.state import postgres_store

logger = logging.getLogger(__name__)


async def _remember(user_id: str, phone: str) -> None:
    """Keep the number for status updates. Without it the chat still works."""
    if not settings.postgres_dsn:
        return
    try:
        await postgres_store.upsert_whatsapp_user(user_id, phone)
    except PostgresStoreError as e:
        logger.warning("WhatsApp user %s not recorded, no status updates for now: %s", user_id, e)


async def reply_to(phone: str, body: str, has_media: bool = False) -> str | None:
    """The reply text for a message from `phone`, or None when there is nothing to answer."""
    if not body.strip():
        return whatsapp.TEXT_ONLY_REPLY if has_media else None

    user_id = whatsapp.user_id_for(phone)
    await _remember(user_id, phone)
    try:
        result = await pipeline.handle_message(user_id, body)
    except UpstreamError as e:
        logger.error("WhatsApp message from %s failed: %s", user_id, e)
        return whatsapp.ERROR_REPLY
    except Exception:
        logger.exception("WhatsApp message from %s failed unexpectedly", user_id)
        return whatsapp.ERROR_REPLY
    finally:
        # The decision row is written in the background; the worker's loop
        # only runs while a task does, so finish it before this task returns.
        await pipeline.drain_decision_writes()
    return text.format_reply(result)
