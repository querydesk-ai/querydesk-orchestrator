r"""
The Celery app: everything after an SR is created runs here, off the /chat
path (PRD architecture step 5).

  notify_status_change   one webhook event -> one notification (retried)
  reconcile              beat, every reconcile_interval_s: GET /sr?updated_since=

The broker is the same Redis as the rest of the app (REDIS_URL). Results are
not stored. Run in dev with the quiet flags, or an idle worker burns the
Upstash free-tier quota (PRD risk 6); Windows needs the solo pool:

    uv run celery -A querydesk.worker worker --pool=solo -l info
        --without-gossip --without-mingle --without-heartbeat   (one line)
    uv run celery -A querydesk.worker beat -l info
"""

import asyncio
import logging
import ssl

from celery import Celery

from querydesk.core.config import settings
from querydesk.core.errors import UpstreamError
from querydesk.models.schemas import SRStatusEvent
from querydesk.services.notifications import notify

logger = logging.getLogger(__name__)

app = Celery("querydesk", broker=settings.redis_url or None)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    result_backend=None,
    # One task at a time per worker, acknowledged only once it has run, so a
    # crash mid-task leaves the event on the queue.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # The webhook route publishes while the ticketing service waits; fail
    # fast so it gets a 503 and retries, instead of hanging on a dead broker.
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.5},
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "reconcile": {"task": "querydesk.reconcile", "schedule": settings.reconcile_interval_s},
    },
    timezone="UTC",
)
if settings.redis_url.startswith("rediss://"):
    app.conf.broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}

# Tasks are sync, the services are async. The shared asyncpg pool, Redis and
# ticketing clients are bound to the loop that created them, so each worker
# process runs every task on one long-lived loop rather than asyncio.run().
_loop: asyncio.AbstractEventLoop | None = None


def _run(coro):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


@app.task(
    name="querydesk.notify_status_change",
    autoretry_for=(UpstreamError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=5,
)
def notify_status_change(event: dict) -> bool:
    e = SRStatusEvent.model_validate(event)
    return _run(
        notify.handle_status_change(
            sr_id=e.sr_id, user_id=e.user_id, to_status=e.to_status, note=e.note
        )
    )


@app.task(name="querydesk.reconcile")
def reconcile() -> int:
    # No retry: the next beat run covers the same window.
    return _run(notify.reconcile())
