"""
Phase 0 scope: prove the FastAPI -> Fireworks round trip works. No RAG,
no retrieval, no routing logic yet — that's Phase 1 onward. This endpoint
just forwards the user's query straight to the LLM so you can confirm
your API key and wiring are correct before building anything on top.
"""

from fastapi import APIRouter, HTTPException

from app.models.schemas import ChatRequest, ChatResponse
from app.services.fireworks_client import FireworksError, chat_completion

router = APIRouter(prefix="/chat", tags=[""])


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        reply = await chat_completion(messages=[{"role": "user", "content": req.query}])
    except FireworksError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(reply=reply, resolution_type="STUB")
