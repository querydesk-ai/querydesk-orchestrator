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
    """
    monkeypatch.setattr(settings, "query_cache_enabled", False)
    redis_store._client = None
    yield
    redis_store._client = None
