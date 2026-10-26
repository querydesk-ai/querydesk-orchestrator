"""
Status notifications (PRD F9, Day 7): turn an SR status change into one
notification for its requester.

Two ways in, one write path:
  - the ticketing service's signed webhook, via the Celery task
    notify_status_change (querydesk.worker);
  - reconcile(), the Celery beat backstop every reconcile_interval_s, which
    asks GET /sr?updated_since= for anything a webhook never delivered. It
    only sees an SR's current status, so a status it skipped past is not
    notified — the webhook is the primary path, this catches what it dropped.

handle_status_change() is idempotent: notifications are unique per SR and
status (migrations/002_notifications.sql), so a retried webhook, a replay or a
reconciliation overlap writes nothing new. Messages are fixed templates — no
LLM — and name the category through the catalog.

A user who chats on WhatsApp also gets each new notification in that chat,
queued as querydesk.whatsapp_send. It follows the write, so the same dedupe
keeps a retried webhook from messaging them twice.
"""

import logging
from datetime import UTC, datetime, timedelta

import anyio

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError, RedisStoreError, TicketingError
from querydesk.services import catalog
from querydesk.services.messaging import whatsapp
from querydesk.services.state import postgres_store, redis_store
from querydesk.services.ticketing import client as ticketing

logger = logging.getLogger(__name__)
notification_log = logging.getLogger("querydesk.notification")

# Where the reconciliation poll got to (ISO-8601, the ticketing service's clock).
WATERMARK_KEY = "reconcile:since"
WATERMARK_TTL_S = 30 * 24 * 3600

# PENDING is where every SR starts, and the user was told that in chat.
_SILENT = frozenset({"PENDING"})

_TEMPLATES = {
    "APPROVED": "was approved. Work will start shortly.",
    "REJECTED": "was rejected.",
    "IN_PROGRESS": "is now in progress.",
    "RESOLVED": "has been resolved.",
    "REVERTED": "was reverted: its window has ended.",
}


def message_for(sr_id: str, status: str, category_name: str | None, note: str | None) -> str:
    subject = f"Your request SR {sr_id}" + (f" ({category_name})" if category_name else "")
    text = f"{subject} {_TEMPLATES.get(status, f'moved to {status}.')}"
    if note and note.strip():
        text += f" Note: {note.strip()}"
    return text


def _lower_statuses(status: str) -> list[str]:
    """Statuses ranked below `status` — the ones a recorded SR may move up from."""
    rank = ticketing.STATUS_RANK.get(status)
    if rank is None:
        return []
    return [s for s, r in ticketing.STATUS_RANK.items() if r < rank]


async def _category_name(sr_id: str, category_id: str | None) -> str | None:
    """
    The category's display name, for the message. The webhook carries no
    category, so ask the ticketing service; a failure only drops the name.
    """
    if category_id is None:
        try:
            sr = await ticketing.get_sr(sr_id)
        except TicketingError as e:
            logger.warning("No category for SR %s, notifying without it: %s", sr_id, e)
            return None
        if sr is None:
            return None
        category_id = sr.category_id
    try:
        return catalog.get(category_id).name
    except catalog.UnknownCategoryError:
        return None


async def handle_status_change(
    *,
    sr_id: str,
    user_id: str,
    to_status: str,
    note: str | None = None,
    category_id: str | None = None,
    source: str = "webhook",
) -> bool:
    """
    Notify `user_id` that SR `sr_id` reached `to_status`. Returns True when a
    new notification was written, False for a silent status or a duplicate.

    Raises PostgresStoreError when the write fails, so the Celery task retries.
    """
    if to_status in _SILENT:
        return False
    name = await _category_name(sr_id, category_id)
    message = message_for(sr_id, to_status, name, note)
    written = await postgres_store.record_status_change(
        sr_id=sr_id,
        user_id=user_id,
        status=to_status,
        message=message,
        lower_statuses=_lower_statuses(to_status),
    )
    notification_log.info(
        "sr_id=%s user_id=%s status=%s source=%s written=%s",
        sr_id,
        user_id,
        to_status,
        source,
        written,
    )
    if written and whatsapp.is_whatsapp_user(user_id) and settings.whatsapp_enabled:
        await _send_on_whatsapp(user_id, message)
    return written


def _enqueue_whatsapp(phone: str, message: str) -> None:
    # Imported here: the worker module imports this one.
    from querydesk.worker import whatsapp_send

    whatsapp_send.delay(phone, message)


async def _send_on_whatsapp(user_id: str, message: str) -> None:
    """
    Queue the notification for the user's WhatsApp chat. The notification is
    already stored, so a failure here is logged, never raised: a retry would
    find it written and send nothing anyway.
    """
    try:
        phone = await postgres_store.whatsapp_phone(user_id)
    except PostgresStoreError as e:
        logger.warning("No WhatsApp number for %s, update not sent: %s", user_id, e)
        return
    if phone is None:
        logger.warning("No WhatsApp number recorded for %s, update not sent", user_id)
        return
    try:
        await anyio.to_thread.run_sync(_enqueue_whatsapp, phone, message)
    except Exception as e:  # kombu/redis raise several unrelated types
        logger.warning("WhatsApp update for %s not queued: %s", user_id, e)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _load_watermark() -> datetime | None:
    try:
        return _parse(await redis_store.get_json(WATERMARK_KEY))
    except RedisStoreError as e:
        logger.warning("Reconcile watermark unavailable, looking back the full window: %s", e)
        return None


async def _save_watermark(value: datetime) -> None:
    try:
        await redis_store.set_json(WATERMARK_KEY, value.isoformat(), WATERMARK_TTL_S)
    except RedisStoreError as e:
        # Harmless: the next run looks further back and the writes dedupe.
        logger.warning("Reconcile watermark not saved: %s", e)


async def reconcile(now: datetime | None = None) -> int:
    """
    Notify every status change since the last run that no webhook delivered.
    Returns how many notifications were written.

    Looks back `reconcile_overlap_s` before the watermark (clock skew between
    the services), or `reconcile_first_lookback_s` when there is none, then
    moves the watermark to the newest `updated_at` seen — or to `now` if
    nothing changed — never backwards. Raises TicketingError if the poll fails; the watermark
    then stays put and the next run covers the same window.
    """
    now = now or datetime.now(UTC)
    watermark = await _load_watermark()
    if watermark is None:
        since = now - timedelta(seconds=settings.reconcile_first_lookback_s)
    else:
        since = watermark - timedelta(seconds=settings.reconcile_overlap_s)

    records = await ticketing.list_updated_since(since)
    written = 0
    newest = None
    for sr in records:
        if sr.user_id is None:
            logger.warning("SR %s has no user_id, cannot notify", sr.id)
        elif await handle_status_change(
            sr_id=sr.id,
            user_id=sr.user_id,
            to_status=sr.status,
            category_id=sr.category_id,
            source="reconcile",
        ):
            written += 1
        updated = _parse(sr.updated_at)
        if updated and (newest is None or updated > newest):
            newest = updated

    advanced = newest or now
    if watermark is not None and watermark > advanced:
        advanced = watermark
    await _save_watermark(advanced)
    logger.info("Reconciled %d SRs since %s: %d notified", len(records), since, written)
    return written
