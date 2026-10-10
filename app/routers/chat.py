"""
POST /chat — the single entry point for the chat page.

Today this is retrieval + a grounded answer. From PRD Day 4 the decision layer
sits between the request and rag.answer(), routing to answer / collect details /
raise SR / escalate / status lookup. That layer goes in its own module; this
router stays a thin HTTP adapter.
"""

from fastapi import APIRouter, HTTPException

from app.models.schemas import ChatRequest, ChatResponse
from app.services import rag
from app.services.fireworks_client import FireworksError
from app.services.qdrant_store import QdrantStoreError

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = await rag.answer(req.query)
    except (FireworksError, QdrantStoreError) as e:
        # Both are upstream dependency failures, not bad input — 502, not 500.
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(
        reply=result.reply,
        resolution_type=result.resolution_type,
        sources=result.sources,
        retrieval_confidence=result.retrieval_confidence,
    )
