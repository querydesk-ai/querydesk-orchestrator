"""
WhatsApp, whichever provider carries it: a private one-to-one chat between
each user and the bot (docs/whatsapp.md).

  user_id_for()  the stable, pseudonymous user id for a phone number
  send()         one reply or update, through the configured provider

The providers each own their API: services/messaging/meta.py (Meta's Cloud
API) and services/messaging/twilio.py. Phone numbers are E.164 between them.
A number never reaches the ticketing service, logs or decision rows: the
pipeline only ever sees user_id_for(phone). It is kept in one place,
whatsapp_users (migrations/004), so status updates can find the chat.
"""

import hashlib
import hmac
import re

from querydesk.core.config import settings
from querydesk.core.errors import WhatsAppError
from querydesk.services.messaging import meta, twilio

USER_PREFIX = "wa-"

TEXT_ONLY_REPLY = "I can only read text messages. Please type your question or request."
ERROR_REPLY = "Something went wrong on our side. Please try again in a minute."


def normalize_phone(raw: str) -> str:
    """E.164 from either provider's form: "whatsapp:+44 7700…" or Meta's "447700…"."""
    return "+" + re.sub(r"\D", "", raw)


def user_id_for(phone: str) -> str:
    """`wa-` + 16 hex chars of HMAC-SHA256(whatsapp_id_secret, phone): stable per number."""
    digest = hmac.new(
        settings.whatsapp_id_secret.encode("utf-8"), phone.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return USER_PREFIX + digest[:16]


def is_whatsapp_user(user_id: str) -> bool:
    return user_id.startswith(USER_PREFIX)


async def send(phone: str, body: str) -> None:
    """Send `body` to `phone` (E.164) through the configured provider. Raises WhatsAppError."""
    if settings.whatsapp_provider == "meta":
        await meta.send(phone, body)
    elif settings.whatsapp_provider == "twilio":
        await twilio.send(phone, body)
    else:
        raise WhatsAppError(f"Unknown WHATSAPP_PROVIDER {settings.whatsapp_provider!r}")
