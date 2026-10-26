"""
/webhooks/whatsapp HTTP contract, for both providers. The Celery publish is
patched and Redis is fakeredis: the route must verify, dedupe and enqueue —
nothing else.
"""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import fakeredis
import httpx
import pytest

from querydesk.api.routes import whatsapp as route
from querydesk.core.config import settings
from querydesk.main import app
from querydesk.services.messaging import meta, twilio
from querydesk.services.state import redis_store

PHONE = "+15551234567"
BODY = "how long does an account lockout last?"


@pytest.fixture(autouse=True)
def common(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_id_secret", "id-secret")
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    monkeypatch.setattr(redis_store, "_client", fakeredis.aioredis.FakeRedis())


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(route, "_enqueue", lambda *args: calls.append(args))
    return calls


def _client() -> httpx.AsyncClient:
    # The app sees its own URL (as behind a tunnel), not the public one.
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://internal:8000")


# --- Meta -----------------------------------------------------------------


@pytest.fixture
def meta_on(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_provider", "meta")
    monkeypatch.setattr(settings, "meta_access_token", "EAAtoken")
    monkeypatch.setattr(settings, "meta_phone_number_id", "1234567890")
    monkeypatch.setattr(settings, "meta_app_secret", "app-secret")
    monkeypatch.setattr(settings, "meta_verify_token", "verify-me")


def _meta_payload(*messages) -> dict:
    value = {"messaging_product": "whatsapp", "messages": list(messages)}
    return {"object": "whatsapp_business_account", "entry": [{"changes": [
        {"field": "messages", "value": value}
    ]}]}  # fmt: skip


def _text(message_id: str, body: str = BODY) -> dict:
    return {"from": PHONE[1:], "id": message_id, "type": "text", "text": {"body": body}}


async def _post_meta(payload: dict, signature: str | None = "sign"):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "sign":
        signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    if signature is not None:
        headers[meta.SIGNATURE_HEADER] = signature
    async with _client() as c:
        return await c.post("/webhooks/whatsapp", content=raw, headers=headers)


@pytest.mark.anyio
async def test_meta_subscription_echoes_the_challenge(meta_on):
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "verify-me",
        "hub.challenge": "1158201444",
    }
    async with _client() as c:
        resp = await c.get("/webhooks/whatsapp", params=params)

    assert resp.status_code == 200
    assert resp.text == "1158201444"


@pytest.mark.anyio
async def test_meta_subscription_with_the_wrong_token_is_403(meta_on):
    params = {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}
    async with _client() as c:
        assert (await c.get("/webhooks/whatsapp", params=params)).status_code == 403


@pytest.mark.anyio
async def test_a_signed_meta_message_is_queued(meta_on, queued):
    resp = await _post_meta(_meta_payload(_text("wamid.1")))

    assert resp.status_code == 200
    assert queued == [(PHONE, BODY, False)]


@pytest.mark.anyio
@pytest.mark.parametrize("signature", [None, "sha256=deadbeef"])
async def test_a_bad_or_missing_meta_signature_is_403(meta_on, queued, signature):
    resp = await _post_meta(_meta_payload(_text("wamid.1")), signature=signature)

    assert resp.status_code == 403
    assert queued == []


@pytest.mark.anyio
async def test_every_message_in_a_meta_batch_is_queued_once(meta_on, queued):
    payload = _meta_payload(_text("wamid.1", "first"), _text("wamid.2", "second"))

    await _post_meta(payload)
    await _post_meta(payload)  # Meta redelivers

    assert queued == [(PHONE, "first", False), (PHONE, "second", False)]


@pytest.mark.anyio
async def test_meta_media_is_flagged_for_the_worker(meta_on, queued):
    image = {"from": PHONE[1:], "id": "wamid.3", "type": "image", "image": {"id": "m"}}

    await _post_meta(_meta_payload(image))

    assert queued == [(PHONE, "", True)]


@pytest.mark.anyio
async def test_meta_receipts_are_acknowledged_and_ignored(meta_on, queued):
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "statuses": [{"id": "wamid.9", "status": "delivered"}]
    }}]}]}  # fmt: skip

    assert (await _post_meta(payload)).status_code == 200
    assert queued == []


@pytest.mark.anyio
async def test_a_meta_broker_failure_is_503_and_lets_the_redelivery_through(meta_on, monkeypatch):
    def broken(*args):
        raise ConnectionError("broker down")

    monkeypatch.setattr(route, "_enqueue", broken)
    assert (await _post_meta(_meta_payload(_text("wamid.1")))).status_code == 503

    calls = []
    monkeypatch.setattr(route, "_enqueue", lambda *args: calls.append(args))
    assert (await _post_meta(_meta_payload(_text("wamid.1")))).status_code == 200
    assert len(calls) == 1


@pytest.mark.anyio
async def test_no_broker_is_503(meta_on, queued, monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")

    assert (await _post_meta(_meta_payload(_text("wamid.1")))).status_code == 503
    assert queued == []


@pytest.mark.anyio
async def test_whatsapp_off_is_404(meta_on, queued, monkeypatch):
    monkeypatch.setattr(settings, "meta_app_secret", "")

    assert (await _post_meta(_meta_payload(_text("wamid.1")))).status_code == 404
    async with _client() as c:
        assert (await c.get("/webhooks/whatsapp")).status_code == 404
    assert queued == []


# --- Twilio ---------------------------------------------------------------

PUBLIC_URL = "https://bot.example.com/webhooks/whatsapp"
TWILIO_MESSAGE = {
    "SmsMessageSid": "SM123",
    "MessageSid": "SM123",
    "AccountSid": "AC1",
    "From": "whatsapp:" + PHONE,
    "To": "whatsapp:+14155238886",
    "Body": BODY,
    "NumMedia": "0",
    "ProfileName": "Sam",
}


@pytest.fixture
def twilio_on(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_account_sid", "AC1")
    monkeypatch.setattr(settings, "twilio_auth_token", "token")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "whatsapp:+14155238886")
    monkeypatch.setattr(settings, "whatsapp_public_url", PUBLIC_URL)


async def _post_twilio(fields: dict, signature: str | None = "sign"):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature == "sign":
        signature = twilio.compute_signature(PUBLIC_URL, fields.items())
    if signature is not None:
        headers[twilio.SIGNATURE_HEADER] = signature
    async with _client() as c:
        return await c.post("/webhooks/whatsapp", content=urlencode(fields), headers=headers)


@pytest.mark.anyio
async def test_a_signed_twilio_message_is_queued_and_answered_with_empty_twiml(twilio_on, queued):
    resp = await _post_twilio(TWILIO_MESSAGE)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    assert resp.text == route.EMPTY_TWIML
    assert queued == [(PHONE, BODY, False)]


@pytest.mark.anyio
@pytest.mark.parametrize("signature", [None, "bm90LWEtc2lnbmF0dXJl"])
async def test_a_bad_or_missing_twilio_signature_is_403(twilio_on, queued, signature):
    assert (await _post_twilio(TWILIO_MESSAGE, signature=signature)).status_code == 403
    assert queued == []


@pytest.mark.anyio
async def test_the_twilio_signature_is_checked_against_the_public_url(twilio_on, queued):
    # Signed for the address the app happens to see: not what Twilio called.
    internal = twilio.compute_signature(
        "http://internal:8000/webhooks/whatsapp", TWILIO_MESSAGE.items()
    )

    assert (await _post_twilio(TWILIO_MESSAGE, signature=internal)).status_code == 403


@pytest.mark.anyio
async def test_a_redelivered_twilio_message_is_handled_once(twilio_on, queued):
    await _post_twilio(TWILIO_MESSAGE)
    await _post_twilio(TWILIO_MESSAGE)

    assert len(queued) == 1


@pytest.mark.anyio
async def test_twilio_media_is_flagged_for_the_worker(twilio_on, queued):
    await _post_twilio({**TWILIO_MESSAGE, "Body": "", "NumMedia": "1", "MediaUrl0": "https://x"})

    assert queued == [(PHONE, "", True)]


@pytest.mark.anyio
async def test_a_signed_non_whatsapp_twilio_request_is_ignored(twilio_on, queued):
    resp = await _post_twilio({"MessageSid": "SM9", "MessageStatus": "delivered", "From": "+1"})

    assert resp.status_code == 200
    assert queued == []


@pytest.mark.anyio
async def test_the_other_providers_webhook_is_404(twilio_on, queued):
    async with _client() as c:
        resp = await c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe"})

    assert resp.status_code == 404
