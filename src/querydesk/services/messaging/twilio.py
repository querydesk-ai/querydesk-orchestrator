"""
The only module that talks to Twilio's WhatsApp API (whatsapp_provider =
"twilio"). Needs a paid account: Twilio's free trial answers inbound
messages with its own auto-reply and never calls a custom webhook.

  verify_signature()  X-Twilio-Signature on an inbound webhook
  send()              POST /Accounts/{sid}/Messages.json, split at 1,600 chars

Numbers are E.164 ("+447700900123") everywhere else; Twilio's "whatsapp:"
prefix is added and stripped here.
"""

import base64
import hashlib
import hmac
from collections.abc import Iterable

import httpx

from querydesk.core.config import settings
from querydesk.core.errors import WhatsAppError
from querydesk.services.messaging.text import split_body

SIGNATURE_HEADER = "X-Twilio-Signature"
API = "https://api.twilio.com/2010-04-01"
# Twilio rejects a WhatsApp body longer than this.
MAX_BODY = 1600
PREFIX = "whatsapp:"


def compute_signature(url: str, params: Iterable[tuple[str, str]]) -> str:
    """
    Twilio's scheme: base64 HMAC-SHA1, under the auth token, of the full URL
    followed by every POST parameter as name+value, sorted by name.
    """
    data = url + "".join(k + v for k, v in sorted(params))
    mac = hmac.new(settings.twilio_auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def verify_signature(url: str, params: Iterable[tuple[str, str]], signature: str | None) -> bool:
    """True only for a matching signature under a configured auth token."""
    if not settings.twilio_auth_token or not signature:
        return False
    return hmac.compare_digest(compute_signature(url, params), signature)


async def send(phone: str, body: str) -> None:
    """
    Send `body` to `phone` (E.164), in several messages if it is longer than
    Twilio allows.

    Raises WhatsAppError: retryable for a network failure, 429 or 5xx; not
    retryable when Twilio rejected the message itself.
    """
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        raise WhatsAppError("Twilio is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)")
    url = f"{API}/Accounts/{settings.twilio_account_sid}/Messages.json"
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    async with httpx.AsyncClient(timeout=settings.twilio_timeout_s, auth=auth) as client:
        for part in split_body(body, MAX_BODY):
            data = {"From": settings.twilio_whatsapp_from, "To": PREFIX + phone, "Body": part}
            try:
                resp = await client.post(url, data=data)
            except httpx.HTTPError as e:
                raise WhatsAppError(f"Twilio unreachable: {e}") from e
            if resp.status_code >= 400:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                raise WhatsAppError(
                    f"Twilio rejected the message ({resp.status_code}): {resp.text[:200]}",
                    retryable=retryable,
                )
