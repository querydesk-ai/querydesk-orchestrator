"""
POST /webhooks/sr-status — the ticketing service's signed status webhook
(PRD contract v1.1).

A thin adapter: check the signature over the raw body, validate, hand the
event to Celery and return 202. No database work happens on the request.
A broker that can't take the event is a 503, so the ticketing service's
outbox retries it; the reconciliation poll is the backstop after that.
"""

import logging

import anyio
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from querydesk.core.config import settings
from querydesk.models.schemas import SRStatusEvent
from querydesk.services.notifications import signing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _enqueue(event: SRStatusEvent) -> None:
    # Imported here so the API process only loads Celery when a webhook arrives.
    from querydesk.worker import notify_status_change

    notify_status_change.delay(event.model_dump())


@router.post("/sr-status", status_code=status.HTTP_202_ACCEPTED)
async def sr_status(request: Request) -> dict[str, str]:
    body = await request.body()
    if not signing.verify(body, request.headers.get(signing.SIGNATURE_HEADER)):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")

    try:
        event = SRStatusEvent.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False)) from e

    if not settings.redis_url:
        raise HTTPException(status_code=503, detail="No broker configured (REDIS_URL)")
    try:
        # .delay() is a blocking publish; keep it off the event loop.
        await anyio.to_thread.run_sync(_enqueue, event)
    except Exception as e:  # kombu/redis raise several unrelated types
        logger.error("Could not enqueue status change for SR %s: %s", event.sr_id, e)
        raise HTTPException(status_code=503, detail="Could not queue the event") from e

    return {"status": "queued"}
