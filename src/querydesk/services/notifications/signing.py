"""
Webhook signatures (PRD contract v1.1): the ticketing service signs each
status webhook's raw body with HMAC-SHA256 under the shared WEBHOOK_SECRET
and sends the lowercase hex digest in X-Signature, with no prefix.

The stand-in ticketing service signs with sign(); the receiver checks with
verify(). Replays need no timestamp: processing a status change is
idempotent (notifications are unique per SR and status).
"""

import hashlib
import hmac

from querydesk.core.config import settings

SIGNATURE_HEADER = "X-Signature"


def sign(body: bytes, secret: str | None = None) -> str:
    key = (settings.webhook_secret if secret is None else secret).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None) -> bool:
    """True only for a matching signature under a configured secret."""
    if not settings.webhook_secret or not signature:
        return False
    return hmac.compare_digest(sign(body), signature.strip().lower())
