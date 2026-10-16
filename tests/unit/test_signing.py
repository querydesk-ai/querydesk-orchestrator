"""Webhook signatures: HMAC-SHA256 hex of the raw body under WEBHOOK_SECRET."""

import hashlib
import hmac

import pytest

from querydesk.core.config import settings
from querydesk.services.notifications import signing

BODY = b'{"sr_id":"1001","user_id":"u1","to_status":"APPROVED"}'


def test_sign_is_lowercase_hex_hmac_sha256_of_the_body():
    expected = hmac.new(b"test-secret", BODY, hashlib.sha256).hexdigest()
    assert signing.sign(BODY) == expected


def test_a_matching_signature_verifies():
    assert signing.verify(BODY, signing.sign(BODY))
    assert signing.verify(BODY, signing.sign(BODY).upper())  # hex case doesn't matter


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "deadbeef",
        signing.sign(BODY + b" "),  # body changed after signing
        signing.sign(BODY, secret="other-secret"),
        "sha256=" + signing.sign(BODY),  # the contract has no prefix
    ],
)
def test_anything_else_is_rejected(signature):
    assert not signing.verify(BODY, signature)


def test_an_empty_secret_rejects_every_webhook(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "")
    assert not signing.verify(BODY, signing.sign(BODY, secret=""))
