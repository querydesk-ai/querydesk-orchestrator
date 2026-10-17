"""
GET /notifications and POST /notifications/read: a user's status-change
messages (PRD F9, Day 8), written by the Day 7 notify task.

A thin adapter over postgres_store. There is no login in v1 (PRD): the
user_id is a parameter, exactly as on POST /chat. Marking read is scoped to
that user_id, so it can't touch anyone else's notifications.
"""

from fastapi import APIRouter, HTTPException, Query

from querydesk.core.errors import UpstreamError
from querydesk.models.schemas import MarkReadRequest, MarkReadResponse, Notification
from querydesk.services.state import postgres_store

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[Notification])
async def list_notifications(
    user_id: str = Query(min_length=1),
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[Notification]:
    try:
        rows = await postgres_store.list_notifications(
            user_id, unread_only=unread_only, limit=limit
        )
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return [Notification(**r) for r in rows]


@router.post("/read", response_model=MarkReadResponse)
async def mark_read(req: MarkReadRequest) -> MarkReadResponse:
    try:
        marked = await postgres_store.mark_read(req.user_id, req.ids)
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return MarkReadResponse(marked=marked)
