"""
/webhooks/whatsapp — where the WhatsApp provider delivers each message a
user sends the bot (whatsapp_provider: "meta" or "twilio").

A thin adapter: verify the provider's signature, drop a message already
seen, hand each one to Celery and answer at once. The reply goes back
through the provider's API from the worker: the pipeline takes several
seconds, longer than a webhook should be held open. A broker or Redis that
can't take the message is a 503, so the provider reports it as failed (Meta
redelivers it) rather than losing it silently.

GET is Meta's one-off check when the webhook URL is saved in the app.
"""

import json
import logging

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from querydesk.core.config import settings
from querydesk.core.errors import RedisStoreError
from querydesk.services.messaging import meta, twilio, whatsapp
from querydesk.services.state import redis_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
# A provider may deliver one message more than once; each id is handled once.
SEEN_TTL_S = 24 * 3600


def _enqueue(phone: str, body: str, has_media: bool) -> None:
    # Imported here so the API process only loads Celery when a message arrives.
    from querydesk.worker import whatsapp_message

    whatsapp_message.delay(phone, body, has_media)


def _require(provider: str) -> None:
    if not settings.whatsapp_enabled or settings.whatsapp_provider != provider:
        raise HTTPException(status_code=404, detail="WhatsApp is not configured")


async def _accept(message_id: str, phone: str, body: str, has_media: bool) -> None:
    """Queue one message for the worker, once per id. Raises 503 if it can't be queued."""
    if not settings.redis_url:
        raise HTTPException(status_code=503, detail="No broker configured (REDIS_URL)")
    seen_key = f"wa:msg:{message_id}"
    try:
        if not await redis_store.set_if_absent(seen_key, SEEN_TTL_S):
            logger.info("WhatsApp message %s already handled", message_id)
            return
    except RedisStoreError as e:
        logger.error("WhatsApp dedupe unavailable for %s: %s", message_id, e)
        raise HTTPException(status_code=503, detail="Could not queue the message") from e

    try:
        # .delay() is a blocking publish; keep it off the event loop.
        await anyio.to_thread.run_sync(_enqueue, phone, body, has_media)
    except Exception as e:  # kombu/redis raise several unrelated types
        logger.error("Could not enqueue WhatsApp message %s: %s", message_id, e)
        try:
            # Not queued, so not handled: let a redelivery through.
            await redis_store.delete(seen_key)
        except RedisStoreError:
            pass
        raise HTTPException(status_code=503, detail="Could not queue the message") from e


@router.get("/whatsapp", response_class=PlainTextResponse)
async def meta_subscription(request: Request) -> str:
    _require("meta")
    q = request.query_params
    if not meta.verify_subscription(q.get("hub.mode"), q.get("hub.verify_token")):
        raise HTTPException(status_code=403, detail="Verify token does not match")
    return q.get("hub.challenge", "")


@router.post("/whatsapp")
async def whatsapp_inbound(request: Request) -> Response:
    if settings.whatsapp_provider == "twilio":
        return await _twilio(request)
    return await _meta(request)


async def _meta(request: Request) -> Response:
    _require("meta")
    raw = await request.body()
    if not meta.verify_signature(raw, request.headers.get(meta.SIGNATURE_HEADER)):
        raise HTTPException(status_code=403, detail="Invalid or missing signature")
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Body is not JSON") from e

    messages, failures = meta.parse(payload if isinstance(payload, dict) else {})
    for failure in failures:
        logger.warning("WhatsApp delivery %s", failure)
    for m in messages:
        await _accept(m.message_id, m.phone, m.body, m.has_media)
    return Response(status_code=200)


async def _twilio(request: Request) -> Response:
    _require("twilio")
    form = await request.form()
    params = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
    # The signature covers the URL Twilio called, which behind a proxy or a
    # tunnel is not the one this process sees.
    url = settings.whatsapp_public_url
    if request.url.query:
        url += "?" + request.url.query
    if not twilio.verify_signature(url, params, request.headers.get(twilio.SIGNATURE_HEADER)):
        raise HTTPException(status_code=403, detail="Invalid or missing Twilio signature")

    fields = dict(params)
    sender = fields.get("From", "")
    message_id = fields.get("MessageSid", "")
    if sender.startswith(twilio.PREFIX) and message_id:
        has_media = fields.get("NumMedia", "0") not in ("", "0")
        await _accept(
            message_id, whatsapp.normalize_phone(sender), fields.get("Body", ""), has_media
        )
    # Anything else signed (a status callback, say) is acknowledged and ignored.
    return Response(content=EMPTY_TWIML, media_type="text/xml")
