"""
The only module that talks to Postgres (the orchestrator's own Neon database,
sr_orchestrator). Nothing else may open a connection to it.

Day 6 records every raised SR and every escalation in sr_requests
(migrations/001_sr_requests.sql, applied by scripts/migrate.py). Every
failure, including a missing POSTGRES_DSN, raises PostgresStoreError; the
caller decides what a failed write means (pipeline.py: an SR is still
returned, an escalation falls back to "contact the desk").
"""

import json

import asyncpg

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError

_pool: asyncpg.Pool | None = None

_INSERT = """
INSERT INTO sr_requests (
    ticket_id, user_id, category_id, details, status, resolution_type,
    requires_approval, retrieval_confidence
) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
RETURNING id
"""


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if not settings.postgres_dsn:
        raise PostgresStoreError("POSTGRES_DSN is not set")
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                settings.postgres_dsn, min_size=1, max_size=settings.postgres_pool_max
            )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
            raise PostgresStoreError(f"Postgres connect failed: {e}") from e
    return _pool


async def aclose() -> None:
    """Close the shared pool (app shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _insert(*args) -> int:
    pool = await _get_pool()
    try:
        return await pool.fetchval(_INSERT, *args)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
        raise PostgresStoreError(f"Postgres insert into sr_requests failed: {e}") from e


async def record_sr(
    *,
    ticket_id: str,
    user_id: str,
    category_id: str,
    details: dict[str, str],
    status: str,
    requires_approval: bool,
    retrieval_confidence: float | None,
) -> int:
    """Record an SR raised in the ticketing service (AUTO_SR). Returns the row id."""
    return await _insert(
        ticket_id,
        user_id,
        category_id,
        json.dumps(details),
        status,
        "AUTO_SR",
        requires_approval,
        retrieval_confidence,
    )


async def record_escalation(
    *,
    user_id: str,
    category_id: str | None,
    details: dict[str, str],
    retrieval_confidence: float | None,
) -> int:
    """Record a message handed to the service desk (MANUAL_ESCALATION, no SR)."""
    return await _insert(
        None,
        user_id,
        category_id,
        json.dumps(details),
        "ESCALATED",
        "MANUAL_ESCALATION",
        False,
        retrieval_confidence,
    )
