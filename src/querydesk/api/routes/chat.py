"""
POST /chat — the single entry point for the chat page.

A thin HTTP adapter: the decision layer and everything it triggers live in
services/pipeline.py. This module only maps the result to the response model
and dependency failures to 502.
"""

from dataclasses import asdict

from fastapi import APIRouter

from querydesk.models.schemas import ChatRequest, ChatResponse
from querydesk.services import pipeline

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    # An UpstreamError propagates to the app-level handler in main.py (logged, 502).
    result = await pipeline.handle_message(req.user_id, req.query)
    return ChatResponse(**asdict(result))
