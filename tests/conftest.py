import pytest

from querydesk.core.config import settings
from querydesk.services.state import redis_store
from querydesk.services.ticketing import client as ticketing


@pytest.fixture
def anyio_backend():
    """
    Run @pytest.mark.anyio tests on asyncio.

    anyio ships its own pytest plugin, so async tests need no extra
    dependency — this fixture just pins the backend so they don't also run
    under trio (which isn't installed).
    """
    return "asyncio"


@pytest.fixture(autouse=True)
def fresh_ticketing_client():
    """
    The ticketing client is shared per process. Each test gets its own event
    loop and often its own stand-in service, so drop the shared client between
    tests rather than reuse one bound to a finished loop or an old fake.
    """
    ticketing._client = None
    yield
    ticketing._client = None


@pytest.fixture(autouse=True)
def no_query_cache(monkeypatch):
    """
    Keep the suite offline even when a developer's .env sets REDIS_URL: the
    query-embedding cache is off unless a test turns it on (with fakeredis).
    Redis and Postgres are unconfigured, so conversation state and SR records
    can never reach a live store; tests that need them use fakes.
    """
    monkeypatch.setattr(settings, "query_cache_enabled", False)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "postgres_dsn", "")
    # Status webhooks: a known secret, and the stand-in never posts anywhere
    # unless a test hands it a target.
    monkeypatch.setattr(settings, "webhook_secret", "test-secret")
    monkeypatch.setattr(settings, "stand_in_webhook_url", "")
    # WhatsApp is off, whatever a developer's .env holds; tests that need it
    # turn it on with fake credentials.
    monkeypatch.setattr(settings, "whatsapp_provider", "meta")
    for name in (
        "meta_access_token",
        "meta_phone_number_id",
        "meta_app_secret",
        "meta_verify_token",
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_whatsapp_from",
        "whatsapp_public_url",
        "whatsapp_id_secret",
    ):
        monkeypatch.setattr(settings, name, "")
    redis_store._client = None
    yield
    redis_store._client = None
