"""
GET /notifications and POST /notifications/read: a user's status-change
messages (PRD F9, Day 8), written by the Day 7 notify task.

A thin adapter over postgres_store. There is no login in v1 (PRD): the
user_id is a parameter, exactly as on POST /chat. Marking read is scoped to
that user_id, so it can't touch anyone else's notifications.
"""

from fastapi import APIRouter, Query

from querydesk.models.schemas import MarkReadRequest, MarkReadResponse, Notification
from querydesk.services.state import postgres_store

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[Notification])
async def list_notifications(
    user_id: str = Query(min_length=1),
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[Notification]:
    # An UpstreamError propagates to the app-level handler in main.py (logged, 502).
    rows = await postgres_store.list_notifications(user_id, unread_only=unread_only, limit=limit)
    return [Notification(**r) for r in rows]


@router.post("/read", response_model=MarkReadResponse)
async def mark_read(req: MarkReadRequest) -> MarkReadResponse:
    marked = await postgres_store.mark_read(req.user_id, req.ids)
    return MarkReadResponse(marked=marked)
