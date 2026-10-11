"""
The only module that talks to the ticketing service (the partner's Spring
Boot service, PRD contract v1.1). Nothing else may call it directly.

Built to the PRD's contract until the OpenAPI file lands in querydesk-docs:
  POST /sr       Idempotency-Key header; 201 = created, 200 = replay of the original
  GET  /sr/{id}
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

__all__ = ["SRCreate", "SRRecord", "TicketingError", "create_sr", "get_sr", "idempotency_key"]


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


def idempotency_key(
    user_id: str, category_id: str, fields: dict[str, str], today: date | None = None
) -> str:
    """
    Deterministic key for one logical request: the same user asking for the
    same thing (same category, same field values) on the same UTC day.

    A retried or double-sent message therefore returns the original SR instead
    of creating a duplicate, while a genuine repeat on another day gets a new
    one — UAT extensions, for example, "do not repeat" and must be re-raised.
    Field values are normalised so "UAT " and "uat" are the same request.
    """
    normalised = {k: " ".join(str(v).lower().split()) for k, v in fields.items()}
    day = (today or datetime.now(UTC).date()).isoformat()
    material = json.dumps([user_id, category_id, normalised, day], sort_keys=True)
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


def _record(resp: httpx.Response, replayed: bool = False) -> SRRecord:
    try:
        body = resp.json()
        return SRRecord(
            id=str(body["id"]),
            status=str(body["status"]),
            category_id=str(body["category_id"]),
            requires_approval=bool(body["requires_approval"]),
            replayed=replayed,
        )
    except (ValueError, KeyError, TypeError) as e:
        raise TicketingError(f"Ticketing response was malformed: {resp.text[:200]}") from e


async def create_sr(sr: SRCreate, key: str) -> SRRecord:
    payload = {
        "user_id": sr.user_id,
        "category_id": sr.category_id,
        "description": sr.description,
        "metadata": sr.metadata,
        "requires_approval": sr.requires_approval,
    }
    try:
        async with _make_client() as client:
            resp = await client.post("/sr", json=payload, headers={"Idempotency-Key": key})
    except httpx.HTTPError as e:
        raise TicketingError(f"Ticketing service unreachable: {e}") from e

    if resp.status_code == 201:
        return _record(resp)
    if resp.status_code == 200:
        return _record(resp, replayed=True)
    raise TicketingError(f"Ticketing create failed: {resp.status_code} {resp.text[:200]}")


async def get_sr(sr_id: str) -> SRRecord:
    try:
        async with _make_client() as client:
            resp = await client.get(f"/sr/{sr_id}")
    except httpx.HTTPError as e:
        raise TicketingError(f"Ticketing service unreachable: {e}") from e

    if resp.status_code != 200:
        raise TicketingError(f"Ticketing lookup failed: {resp.status_code} {resp.text[:200]}")
    return _record(resp)
