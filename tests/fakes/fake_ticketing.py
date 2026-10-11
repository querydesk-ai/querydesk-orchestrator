"""
A stand-in for the partner's ticketing service, built to PRD contract v1.1.

It exists so the orchestrator can be tested end to end before the real
Spring Boot service does: in-process in tests (via ASGITransport) and as a
real server for manual runs:

    uv run uvicorn tests.fakes.fake_ticketing:app --port 8081

It implements only what the orchestrator uses, plus the PRD status
transitions so tests can close an SR — not webhooks, status history or the
revert scheduler. State is in memory and lost on restart.
"""

from datetime import UTC, datetime
from itertools import count

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from querydesk.services import catalog

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


def create_app() -> FastAPI:
    """A fresh app with empty state — tests build one each so they don't share SRs."""
    app = FastAPI(title="Fake ticketing service (PRD v1.1 stand-in)")
    store: dict[int, dict] = {}
    by_key: dict[str, int] = {}
    ids = count(1001)

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
    def change_status(sr_id: int, change: StatusIn) -> dict:
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
        return record

    @app.get("/sr")
    def list_srs(user_id: str | None = None, status: str | None = None) -> list[dict]:
        return [
            r
            for r in store.values()
            if (user_id is None or r["user_id"] == user_id)
            and (status is None or r["status"] == status)
        ]

    return app


app = create_app()
