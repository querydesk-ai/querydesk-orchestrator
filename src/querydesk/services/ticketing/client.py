"""
The only module that talks to the ticketing service (the partner's Spring
Boot service, PRD contract v1.1). Nothing else may call it directly.

Built to the PRD's contract until the OpenAPI file lands in querydesk-docs:
  POST /sr       Idempotency-Key header; 201 = created, 200 = replay of the original
  GET  /sr/{id}       404 = no such SR (None, not an error)
  GET  /sr?user_id=…   a user's SRs (the duplicate check, PRD F5)
  GET  /sr?updated_since=…   SRs changed since a time (the Day 7 reconciliation poll)
Statuses follow the PRD state machine; RESOLVED, REJECTED and REVERTED are final.
Field names follow the PRD's service_requests table — re-check them against
the OpenAPI file when it exists.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx

from querydesk.core.config import settings
from querydesk.core.errors import TicketingError

__all__ = [
    "TERMINAL_STATUSES",
    "aclose",
    "SRCreate",
    "SRRecord",
    "TicketingError",
    "create_sr",
    "get_sr",
    "idempotency_key",
    "list_open_srs",
    "list_updated_since",
    "STATUS_RANK",
    "next_key",
]

# An SR in one of these is closed: a repeat of the same request is a new SR.
TERMINAL_STATUSES = frozenset({"RESOLVED", "REJECTED", "REVERTED"})
# How far along the PRD state machine a status is. Transitions only move up,
# so a webhook for a lower status than the one recorded arrived out of order.
STATUS_RANK = {
    "PENDING": 0,
    "APPROVED": 1,
    "REJECTED": 1,
    "IN_PROGRESS": 2,
    "RESOLVED": 3,
    "REVERTED": 4,
}
# How many closed SRs create_sr will step past for one key in one day.
MAX_REOPENS = 10


@dataclass
class SRCreate:
    user_id: str
    category_id: str
    description: str
    requires_approval: bool
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class SRRecord:
    id: str
    status: str
    category_id: str
    requires_approval: bool
    # True when the service returned an existing SR for this idempotency key.
    replayed: bool = False
    user_id: str | None = None
    # ISO-8601 as the service sent it; the reconciliation poll's watermark.
    updated_at: str | None = None


def idempotency_key(
    user_id: str, category_id: str, fields: dict[str, str], today: date | None = None
) -> str:
    """
    Deterministic key for one logical request: the same user asking for the
    same thing (same category, same field values) on the same UTC day.

    A retried or double-sent message therefore returns the original SR instead
    of creating a duplicate, while a genuine repeat on another day gets a new
    one — UAT extensions, for example, "do not repeat" and must be re-raised.
    A repeat on the same day after the SR has closed gets a new one too:
    create_sr() steps to next_key() off the closed SR's number. Field values
    are normalised so "UAT " and "uat" are the same request.
    """
    normalised = {k: " ".join(str(v).lower().split()) for k, v in fields.items()}
    day = (today or datetime.now(UTC).date()).isoformat()
    material = json.dumps([user_id, category_id, normalised, day], sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def next_key(key: str, closed_sr_id: str) -> str:
    """
    The key for the same request raised again after SR `closed_sr_id` closed.

    Derived from the closed SR's number, so it is still deterministic: a
    resend of the reopened request lands on the same key and replays the new
    SR instead of creating a third.
    """
    material = f"{key}:after:{closed_sr_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _make_client() -> httpx.AsyncClient:
    """One place to build the HTTP client — tests swap in an in-process transport."""
    if not settings.ticketing_base_url:
        raise TicketingError(
            "TICKETING_BASE_URL is not set — point it at the ticketing service "
            "(or the stand-in: uv run uvicorn tests.fakes.fake_ticketing:app --port 8081)."
        )
    return httpx.AsyncClient(
        base_url=settings.ticketing_base_url, timeout=settings.ticketing_timeout_s
    )


# One client for the process, so connections to the ticketing service are
# reused across messages. Built on first use; main.py's lifespan closes it.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = _make_client()
    return _client


async def aclose() -> None:
    """Close the shared client (app shutdown; tests between event loops)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _from_body(body: dict, replayed: bool = False) -> SRRecord:
    return SRRecord(
        id=str(body["id"]),
        status=str(body["status"]),
        category_id=str(body["category_id"]),
        requires_approval=bool(body["requires_approval"]),
        replayed=replayed,
        user_id=str(body["user_id"]) if body.get("user_id") is not None else None,
        updated_at=str(body["updated_at"]) if body.get("updated_at") is not None else None,
    )


def _record(resp: httpx.Response, replayed: bool = False) -> SRRecord:
    try:
        return _from_body(resp.json(), replayed)
    except (ValueError, KeyError, TypeError) as e:
        raise TicketingError(f"Ticketing response was malformed: {resp.text[:200]}") from e


async def _post(client: httpx.AsyncClient, payload: dict, key: str) -> SRRecord:
    try:
        resp = await client.post("/sr", json=payload, headers={"Idempotency-Key": key})
    except httpx.HTTPError as e:
        raise TicketingError(f"Ticketing service unreachable: {e}") from e

    if resp.status_code == 201:
        return _record(resp)
    if resp.status_code == 200:
        return _record(resp, replayed=True)
    raise TicketingError(f"Ticketing create failed: {resp.status_code} {resp.text[:200]}")


async def create_sr(sr: SRCreate, key: str) -> SRRecord:
    """
    Create an SR, or return the open one this key already made.

    A replay of a *closed* SR means the user is asking again after it was
    resolved, rejected or reverted — a new request, so step to the key
    chained off that SR's number and try again.
    """
    payload = {
        "user_id": sr.user_id,
        "category_id": sr.category_id,
        "description": sr.description,
        "metadata": sr.metadata,
        "requires_approval": sr.requires_approval,
    }
    client = _get_client()
    for _ in range(MAX_REOPENS + 1):
        record = await _post(client, payload, key)
        if not (record.replayed and record.status in TERMINAL_STATUSES):
            return record
        key = next_key(key, record.id)
    raise TicketingError(
        f"Gave up after {MAX_REOPENS} closed SRs for the same request today (last: {record.id})"
    )


async def get_sr(sr_id: str) -> SRRecord | None:
    """The SR, or None if the ticketing service has no SR with this id."""
    client = _get_client()
    try:
        resp = await client.get(f"/sr/{sr_id}")
    except httpx.HTTPError as e:
        raise TicketingError(f"Ticketing service unreachable: {e}") from e

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise TicketingError(f"Ticketing lookup failed: {resp.status_code} {resp.text[:200]}")
    return _record(resp)


async def _list(params: dict[str, str]) -> list[SRRecord]:
    client = _get_client()
    try:
        resp = await client.get("/sr", params=params)
    except httpx.HTTPError as e:
        raise TicketingError(f"Ticketing service unreachable: {e}") from e

    if resp.status_code != 200:
        raise TicketingError(f"Ticketing list failed: {resp.status_code} {resp.text[:200]}")
    try:
        return [_from_body(body) for body in resp.json()]
    except (ValueError, KeyError, TypeError) as e:
        raise TicketingError(f"Ticketing response was malformed: {resp.text[:200]}") from e


async def list_open_srs(user_id: str) -> list[SRRecord]:
    """The user's SRs that aren't closed yet (not in TERMINAL_STATUSES)."""
    records = await _list({"user_id": user_id})
    return [r for r in records if r.status not in TERMINAL_STATUSES]


async def list_updated_since(since: datetime) -> list[SRRecord]:
    """Every SR whose status changed at or after `since` (any user)."""
    return await _list({"updated_since": since.astimezone(UTC).isoformat()})
