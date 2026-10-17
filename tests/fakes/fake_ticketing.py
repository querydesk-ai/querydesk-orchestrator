r"""
A stand-in for the ticketing service, built to contract v1.1
(openapi.yaml in querydesk-docs).

It exists so the orchestrator can be tested end to end before the real
Spring Boot service does: in-process in tests (via ASGITransport) and as a
real server for manual runs:

    uv run uvicorn tests.fakes.fake_ticketing:app --port 8081

It implements only what the orchestrator uses, plus the PRD status
transitions so tests (and Swagger at /docs, playing the approver) can move an
SR. Each status change is sent as a signed webhook to STAND_IN_WEBHOOK_URL
when it is set, with a few quick retries — not the real service's outbox,
status history or revert scheduler. State is in memory and lost on restart.

    STAND_IN_WEBHOOK_URL=http://127.0.0.1:8000/webhooks/sr-status \
        uv run uvicorn tests.fakes.fake_ticketing:app --port 8081
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from itertools import count

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from querydesk.core.config import settings
from querydesk.services import catalog
from querydesk.services.notifications import signing

logger = logging.getLogger(__name__)
WEBHOOK_ATTEMPTS = 3

# PRD transitions: from-status -> allowed to-statuses. PENDING depends on
# requires_approval, so it is decided per SR in change_status().
TRANSITIONS = {
    "APPROVED": {"IN_PROGRESS"},
    "IN_PROGRESS": {"RESOLVED"},
    "RESOLVED": {"REVERTED"},
}


class SRIn(BaseModel):
    user_id: str
    category_id: str
    description: str
    requires_approval: bool
    metadata: dict = Field(default_factory=dict)


class StatusIn(BaseModel):
    status: str
    note: str | None = None
    changed_by: str | None = None


async def send_webhook(
    url: str, event: dict, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """POST a signed status webhook, retrying briefly. True once it is accepted."""
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json", signing.SIGNATURE_HEADER: signing.sign(body)}
    async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
        for attempt in range(WEBHOOK_ATTEMPTS):
            try:
                resp = await client.post(url, content=body, headers=headers)
                if resp.status_code < 300:
                    return True
                logger.warning("Webhook for SR %s got %s", event["sr_id"], resp.status_code)
            except httpx.HTTPError as e:
                logger.warning("Webhook for SR %s failed: %s", event["sr_id"], e)
            await asyncio.sleep(0.5 * (attempt + 1))
    return False


def create_app(
    webhook_url: str | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
    first_id: int = 1001,
) -> FastAPI:
    """A fresh app with empty state — tests build one each so they don't share SRs."""
    app = FastAPI(title="Fake ticketing service (PRD v1.1 stand-in)")
    webhook_url = settings.stand_in_webhook_url if webhook_url is None else webhook_url
    store: dict[int, dict] = {}
    by_key: dict[str, int] = {}
    ids = count(first_id)

    @app.post("/sr", status_code=201)
    def create_sr(
        sr: SRIn, response: Response, idempotency_key: str | None = Header(default=None)
    ) -> dict:
        if sr.category_id not in catalog.all_categories():
            raise HTTPException(status_code=400, detail=f"Unknown category {sr.category_id!r}")

        if idempotency_key and idempotency_key in by_key:
            # Contract v1.1: a repeated key returns the original SR with 200.
            response.status_code = 200
            return store[by_key[idempotency_key]]

        now = datetime.now(UTC).isoformat()
        sr_id = next(ids)
        record = {
            "id": sr_id,
            **sr.model_dump(),
            "status": "PENDING",
            "idempotency_key": idempotency_key,
            "revert_at": None,
            "created_at": now,
            "updated_at": now,
        }
        store[sr_id] = record
        if idempotency_key:
            by_key[idempotency_key] = sr_id
        return record

    @app.get("/sr/{sr_id}")
    def get_sr(sr_id: int) -> dict:
        if sr_id not in store:
            raise HTTPException(status_code=404, detail="SR not found")
        return store[sr_id]

    @app.patch("/sr/{sr_id}/status")
    def change_status(sr_id: int, change: StatusIn, background: BackgroundTasks) -> dict:
        if sr_id not in store:
            raise HTTPException(status_code=404, detail="SR not found")
        record = store[sr_id]
        current, target = record["status"], change.status
        allowed = TRANSITIONS.get(current, set())
        if current == "PENDING":
            allowed = {"APPROVED", "REJECTED"} if record["requires_approval"] else {"IN_PROGRESS"}
        if target not in allowed:
            raise HTTPException(status_code=409, detail=f"Cannot move {current} -> {target}")
        record["status"] = target
        record["updated_at"] = datetime.now(UTC).isoformat()
        if webhook_url:
            event = {
                "sr_id": sr_id,
                "user_id": record["user_id"],
                "from_status": current,
                "to_status": target,
                "note": change.note,
                "changed_at": record["updated_at"],
            }
            background.add_task(send_webhook, webhook_url, event, webhook_transport)
        return record

    @app.get("/sr")
    def list_srs(
        user_id: str | None = None,
        status: str | None = None,
        updated_since: datetime | None = None,
        page: int = Query(default=0, ge=0),
        size: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        statuses = set(status.split(",")) if status else None
        matches = sorted(
            (
                r
                for r in store.values()
                if (user_id is None or r["user_id"] == user_id)
                and (statuses is None or r["status"] in statuses)
                and (
                    updated_since is None
                    or datetime.fromisoformat(r["updated_at"]) >= updated_since
                )
            ),
            key=lambda r: (r["updated_at"], r["id"]),
        )
        return {
            "items": matches[page * size : (page + 1) * size],
            "page": page,
            "size": size,
            "total_items": len(matches),
            "total_pages": -(-len(matches) // size),
        }

    return app


# The served stand-in numbers SRs from the clock (seconds since 2026-01-01), so a
# restart never reuses an id. The real service never does, and the orchestrator
# relies on it: notifications are unique per (sr_id, status), so a reused id's
# status changes would be dropped as duplicates.
app = create_app(first_id=int(time.time()) - 1_767_225_600)
