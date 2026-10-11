"""
POST /chat — the single entry point for the chat page.

A thin HTTP adapter: the decision layer and everything it triggers live in
services/pipeline.py. This module only maps the result to the response model
and dependency failures to 502.
"""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from querydesk.core.errors import UpstreamError
from querydesk.models.schemas import ChatRequest, ChatResponse
from querydesk.services import pipeline

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = await pipeline.handle_message(req.user_id, req.query)
    except UpstreamError as e:
        # Any dependency failure (Fireworks, Qdrant, ticketing) is a 502: the
        # request was fine, something behind us wasn't. Catching the family
        # means a new dependency needs no change here.
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ChatResponse(**asdict(result))
