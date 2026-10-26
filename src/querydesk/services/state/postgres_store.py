"""
The only module that talks to Postgres (the orchestrator's own Neon database,
sr_orchestrator). Nothing else may open a connection to it.

Day 6 records every raised SR and every escalation in sr_requests
(migrations/001_sr_requests.sql, applied by scripts/migrate.py); Day 7 adds
one notifications row per status change (002_notifications.sql), and every
decision the pipeline makes goes to decision_logs (003, PRD F10), and
whatsapp_users (004) maps a WhatsApp user id back to its number. Every
failure, including a missing POSTGRES_DSN, raises PostgresStoreError; the
caller decides what a failed write means (pipeline.py: an SR is still
returned, an escalation falls back to "contact the desk").
"""

import json
from datetime import datetime

import asyncpg

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError

_pool: asyncpg.Pool | None = None

_INSERT = """
INSERT INTO sr_requests (
    ticket_id, user_id, category_id, details, status, resolution_type,
    requires_approval, retrieval_confidence, last_notified_status
) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
RETURNING id
"""

_INSERT_NOTIFICATION = """
INSERT INTO notifications (user_id, sr_id, status, message)
VALUES ($1, $2, $3, $4)
ON CONFLICT (sr_id, status) DO NOTHING
RETURNING id
"""

_INSERT_DECISION = """
INSERT INTO decision_logs (
    user_id, query, turn, top_dense_score, router_top_two, jev_output, engine,
    decision, rule, category_id, sr_id, error, latency_ms, details, created_at
) VALUES (
    $1, $2, $3, $4, $5::jsonb, NULL, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb, $14
)
RETURNING id
"""

# The decision record's own columns; everything else goes to `details`.
_DECISION_COLUMNS = (
    "user_id",
    "query",
    "turn",
    "top_dense_score",
    "router_top_two",
    "engine",
    "decision",
    "rule",
    "category_id",
    "sr_id",
    "error",
    "latency_ms",
)

# A user's notifications, newest first; $2 = true keeps only the unread ones.
_LIST_NOTIFICATIONS = """
SELECT id, sr_id, status, message, created_at, read_at
FROM notifications
WHERE user_id = $1 AND (NOT $2 OR read_at IS NULL)
ORDER BY created_at DESC, id DESC
LIMIT $3
"""

# Scoped by user_id: nobody can mark someone else's notifications read.
_MARK_READ = """
UPDATE notifications SET read_at = now()
WHERE user_id = $1 AND id = ANY($2::bigint[]) AND read_at IS NULL
"""

_UPSERT_WHATSAPP_USER = """
INSERT INTO whatsapp_users (user_id, phone) VALUES ($1, $2)
ON CONFLICT (user_id) DO UPDATE SET phone = EXCLUDED.phone, last_seen_at = now()
"""

_WHATSAPP_PHONE = "SELECT phone FROM whatsapp_users WHERE user_id = $1"

# Moves the recorded SR forward only: $3 lists the statuses ranked below the
# new one, so an out-of-order webhook never sets an SR back.
_ADVANCE_SR = """
UPDATE sr_requests
SET status = $2, last_notified_status = $2, updated_at = now()
WHERE ticket_id = $1 AND status = ANY($3::text[])
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


_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


async def _insert(*args) -> int:
    pool = await _get_pool()
    try:
        return await pool.fetchval(_INSERT, *args)
    except _ERRORS as e:
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
    """
    Record an SR raised in the ticketing service (AUTO_SR). Returns the row id.

    The user was just told about the SR in chat, so its creation status
    counts as notified.
    """
    return await _insert(
        ticket_id,
        user_id,
        category_id,
        json.dumps(details),
        status,
        "AUTO_SR",
        requires_approval,
        retrieval_confidence,
        status,
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
        None,
    )


async def record_decision(record: dict, *, at: datetime) -> int:
    """
    Write one decision record (the dict pipeline._log() logs) to decision_logs,
    made at `at`. Returns the row id. Keys that aren't columns are kept in
    `details`.
    """
    details = {k: v for k, v in record.items() if k not in _DECISION_COLUMNS}
    pool = await _get_pool()
    try:
        return await pool.fetchval(
            _INSERT_DECISION,
            record["user_id"],
            record["query"],
            record["turn"],
            record["top_dense_score"],
            json.dumps(record["router_top_two"]) if record["router_top_two"] is not None else None,
            record["engine"],
            record["decision"],
            record["rule"],
            record["category_id"],
            record["sr_id"],
            record["error"],
            json.dumps(record["latency_ms"]),
            json.dumps(details),
            at,
        )
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres insert into decision_logs failed: {e}") from e


async def record_status_change(
    *, sr_id: str, user_id: str, status: str, message: str, lower_statuses: list[str]
) -> bool:
    """
    Write the notification for SR `sr_id` reaching `status`, once.

    Returns False when this SR already had a notification for this status (a
    retried webhook, a replay, a reconciliation overlap): nothing is written.
    Otherwise the notification is stored and, in the same transaction, the
    raised SR in sr_requests moves to `status` if it is still at one of
    `lower_statuses`. An SR with no sr_requests row (its Day 6 write failed)
    is still notified.
    """
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            row_id = await conn.fetchval(_INSERT_NOTIFICATION, user_id, sr_id, status, message)
            if row_id is None:
                return False
            await conn.execute(_ADVANCE_SR, sr_id, status, lower_statuses)
            return True
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres notification write failed: {e}") from e


async def upsert_whatsapp_user(user_id: str, phone: str) -> None:
    """Remember the WhatsApp number behind `user_id` (migrations/004), and when it last wrote."""
    pool = await _get_pool()
    try:
        await pool.execute(_UPSERT_WHATSAPP_USER, user_id, phone)
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres whatsapp_users write failed: {e}") from e


async def whatsapp_phone(user_id: str) -> str | None:
    """The WhatsApp number behind `user_id`, or None if it never wrote in."""
    pool = await _get_pool()
    try:
        return await pool.fetchval(_WHATSAPP_PHONE, user_id)
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres whatsapp_users read failed: {e}") from e


async def list_notifications(user_id: str, *, unread_only: bool, limit: int) -> list[dict]:
    """A user's notifications, newest first (Day 8, GET /notifications)."""
    pool = await _get_pool()
    try:
        rows = await pool.fetch(_LIST_NOTIFICATIONS, user_id, unread_only, limit)
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres notifications read failed: {e}") from e
    return [dict(r) for r in rows]


async def mark_read(user_id: str, ids: list[int]) -> int:
    """Mark the user's notifications `ids` read. Returns how many changed."""
    if not ids:
        return 0
    pool = await _get_pool()
    try:
        result = await pool.execute(_MARK_READ, user_id, ids)
    except _ERRORS as e:
        raise PostgresStoreError(f"Postgres notifications update failed: {e}") from e
    # asyncpg returns the command tag, e.g. "UPDATE 2".
    return int(result.split()[-1])
