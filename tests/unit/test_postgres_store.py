"""
sr_requests writes and their error mapping. No live database: asyncpg's
create_pool is replaced with a fake pool that records each insert.
"""

import json

import asyncpg
import pytest

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError, UpstreamError
from querydesk.services.state import postgres_store


class FakePool:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []
        self.closed = False

    async def fetchval(self, sql, *args):
        if self.error:
            raise self.error
        self.calls.append((sql, args))
        return len(self.calls)

    async def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()

    async def create_pool(dsn, **kwargs):
        return fake

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://fake")
    monkeypatch.setattr(postgres_store.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(postgres_store, "_pool", None)
    return fake


@pytest.mark.anyio
async def test_record_sr_inserts_an_auto_sr_row(pool):
    row_id = await postgres_store.record_sr(
        ticket_id="1001",
        user_id="u1",
        category_id="it_uat_extension",
        details={"query": "keep UAT-2 up", "fields": {"environment": "UAT-2"}},
        status="PENDING_APPROVAL",
        requires_approval=True,
        retrieval_confidence=0.61,
    )

    assert row_id == 1
    [(sql, args)] = pool.calls
    assert "INSERT INTO sr_requests" in sql
    ticket_id, user_id, category_id, details, status, rtype, approval, confidence = args
    assert (ticket_id, user_id, category_id) == ("1001", "u1", "it_uat_extension")
    assert json.loads(details)["fields"] == {"environment": "UAT-2"}
    assert (status, rtype, approval, confidence) == ("PENDING_APPROVAL", "AUTO_SR", True, 0.61)


@pytest.mark.anyio
async def test_record_escalation_has_no_ticket_and_is_manual(pool):
    await postgres_store.record_escalation(
        user_id="u1", category_id=None, details={"query": "?"}, retrieval_confidence=0.17
    )

    [(_, args)] = pool.calls
    assert args[0] is None  # ticket_id
    assert args[2] is None  # category_id
    assert args[4:7] == ("ESCALATED", "MANUAL_ESCALATION", False)


@pytest.mark.anyio
async def test_the_pool_is_created_once_and_closed_on_shutdown(pool):
    for _ in range(2):
        await postgres_store.record_escalation(
            user_id="u1", category_id=None, details={}, retrieval_confidence=None
        )
    assert postgres_store._pool is pool

    await postgres_store.aclose()

    assert pool.closed and postgres_store._pool is None


@pytest.mark.anyio
async def test_missing_dsn_raises_before_connecting(monkeypatch):
    monkeypatch.setattr(postgres_store, "_pool", None)
    # conftest leaves POSTGRES_DSN empty.
    with pytest.raises(PostgresStoreError, match="POSTGRES_DSN"):
        await postgres_store.record_escalation(
            user_id="u1", category_id=None, details={}, retrieval_confidence=None
        )


@pytest.mark.anyio
@pytest.mark.parametrize("error", [OSError("refused"), asyncpg.InvalidPasswordError("bad")])
async def test_connect_failure_is_a_postgres_store_error(monkeypatch, error):
    async def create_pool(dsn, **kwargs):
        raise error

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://fake")
    monkeypatch.setattr(postgres_store.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(postgres_store, "_pool", None)

    with pytest.raises(PostgresStoreError):
        await postgres_store.record_escalation(
            user_id="u1", category_id=None, details={}, retrieval_confidence=None
        )
    assert postgres_store._pool is None  # the next message retries the connect


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [asyncpg.UndefinedTableError("no sr_requests"), asyncpg.InterfaceError("closed"), OSError()],
)
async def test_insert_failure_is_a_postgres_store_error(pool, error):
    pool.error = error
    with pytest.raises(PostgresStoreError):
        await postgres_store.record_sr(
            ticket_id="1",
            user_id="u1",
            category_id="c",
            details={},
            status="PENDING",
            requires_approval=False,
            retrieval_confidence=None,
        )


def test_postgres_store_error_is_an_upstream_error():
    assert issubclass(PostgresStoreError, UpstreamError)
