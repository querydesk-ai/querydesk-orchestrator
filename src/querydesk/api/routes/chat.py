"""
POST /chat — the single entry point for the chat page.

Today this is retrieval + a grounded answer. From PRD Day 4 the decision layer
sits between the request and rag.answer(), routing to answer / collect details /
raise SR / escalate / status lookup. That layer goes in its own module; this
router stays a thin HTTP adapter.
"""

from fastapi import APIRouter, HTTPException

from querydesk.core.errors import UpstreamError
from querydesk.models.schemas import ChatRequest, ChatResponse
from querydesk.services import rag

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = await rag.answer(req.query)
    except UpstreamError as e:
        # Any dependency failure (Fireworks, Qdrant, future ticketing client)
        # is a 502: the request was fine, something behind us wasn't. Catching
        # the family means a new dependency needs no change here.
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ChatResponse(
        reply=result.reply,
        resolution_type=result.resolution_type,
        sources=result.sources,
        retrieval_confidence=result.retrieval_confidence,
    )
