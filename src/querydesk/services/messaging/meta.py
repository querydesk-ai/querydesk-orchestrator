"""
The only module that talks to Meta's WhatsApp Cloud API (whatsapp_provider =
"meta", graph.facebook.com). Meta's free test number can chat with up to
five recipient numbers added in the app, which is what the demo uses.

  verify_subscription()  Meta's one-off GET when the webhook URL is saved
  verify_signature()     X-Hub-Signature-256 on every webhook POST
  parse()                a webhook body -> the messages in it, and failed deliveries
  send()                 POST /{phone_number_id}/messages, split at 4,096 chars

Numbers are E.164 ("+447700900123") everywhere else; Meta's wa_id is the
same digits without the "+".
"""

import hashlib
import hmac
from dataclasses import dataclass

import httpx

from querydesk.core.config import settings
from querydesk.core.errors import WhatsAppError
from querydesk.services.messaging.text import split_body

SIGNATURE_HEADER = "X-Hub-Signature-256"
GRAPH = "https://graph.facebook.com"
# Meta rejects a text body longer than this.
MAX_BODY = 4096
# Graph API error codes that mean "slow down", not "this message is wrong".
_THROTTLED = frozenset({4, 80007, 130429, 131048, 131056})


@dataclass(frozen=True)
class Inbound:
    phone: str
    message_id: str
    body: str
    has_media: bool


def verify_subscription(mode: str | None, token: str | None) -> bool:
    """Meta's GET ?hub.mode=subscribe&hub.verify_token=…: True when the token matches."""
    if mode != "subscribe" or not settings.meta_verify_token or not token:
        return False
    return hmac.compare_digest(token, settings.meta_verify_token)


def verify_signature(body: bytes, header: str | None) -> bool:
    """ "sha256=" + hex HMAC-SHA256 of the raw body under the app secret."""
    if not settings.meta_app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.meta_app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def parse(payload: dict) -> tuple[list[Inbound], list[str]]:
    """
    The user messages in one webhook body, and a description of each message
    of ours Meta reports as failed (e.g. 131047, outside the 24-hour window).
    Read receipts and other events are ignored.
    """
    messages: list[Inbound] = []
    failures: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                sender, message_id = m.get("from"), m.get("id")
                if not sender or not message_id:
                    continue
                is_text = m.get("type") == "text"
                body = (m.get("text") or {}).get("body", "") if is_text else ""
                messages.append(Inbound("+" + sender, message_id, body, not is_text))
            for s in value.get("statuses") or []:
                if s.get("status") == "failed":
                    errors = s.get("errors") or [{}]
                    codes = ", ".join(
                        f"{e.get('code')} {e.get('title', '')}".strip() for e in errors
                    )
                    failures.append(f"message {s.get('id')} failed: {codes}")
    return messages, failures


def _retryable(resp: httpx.Response) -> bool:
    if resp.status_code == 429 or resp.status_code >= 500:
        return True
    try:
        code = resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return False
    return code in _THROTTLED


async def send(phone: str, body: str) -> None:
    """
    Send `body` to `phone` (E.164) as text messages.

    Raises WhatsAppError: retryable for a network failure, throttling or a
    5xx; not retryable when Meta rejected the message (an expired token, a
    number not on the test number's recipient list, outside the 24-hour window).
    """
    if not (settings.meta_access_token and settings.meta_phone_number_id):
        raise WhatsAppError("Meta is not configured (META_ACCESS_TOKEN / META_PHONE_NUMBER_ID)")
    url = f"{GRAPH}/{settings.meta_graph_version}/{settings.meta_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    async with httpx.AsyncClient(timeout=settings.meta_timeout_s, headers=headers) as client:
        for part in split_body(body, MAX_BODY):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone.removeprefix("+"),
                "type": "text",
                "text": {"preview_url": False, "body": part},
            }
            try:
                resp = await client.post(url, json=payload)
            except httpx.HTTPError as e:
                raise WhatsAppError(f"Meta unreachable: {e}") from e
            if resp.status_code >= 400:
                raise WhatsAppError(
                    f"Meta rejected the message ({resp.status_code}): {resp.text[:300]}",
                    retryable=_retryable(resp),
                )
