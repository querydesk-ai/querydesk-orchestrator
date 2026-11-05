"""
GET /history: a user's recent chat, so the chat page can show it again after a
reload.

A thin adapter over services/history.py. There is no login in v1 (PRD): the
user_id is a parameter, exactly as on POST /chat and GET /notifications, so
anyone who knows a user id can read that user's recent messages.
"""

from fastapi import APIRouter, Query

from querydesk.models.schemas import HistoryTurn
from querydesk.services import history

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=list[HistoryTurn])
async def get_history(user_id: str = Query(min_length=1)) -> list[HistoryTurn]:
    return [HistoryTurn(**t) for t in await history.load(user_id)]
