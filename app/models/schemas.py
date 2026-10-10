from enum import StrEnum

from pydantic import BaseModel, Field


class ResolutionType(StrEnum):
    """
    How a message was resolved.

    AUTO_SR is deliberately absent: it arrives with the decision layer on PRD
    Day 4. Only add a value once the code can actually emit it, so the API
    contract never advertises an outcome that can't happen.
    """

    RAG = "RAG"
    MANUAL_ESCALATION = "MANUAL_ESCALATION"


class ChatRequest(BaseModel):
    user_id: str
    query: str


class Source(BaseModel):
    """A KB chunk an answer was grounded in. Every RAG answer cites at least one."""

    title: str
    source_path: str
    source_url: str | None = None
    category_id: str | None = None
    snippet: str
    score: float


class ChatResponse(BaseModel):
    reply: str
    resolution_type: ResolutionType
    sources: list[Source] = Field(default_factory=list)
    # Top dense cosine similarity, never a fused RRF score. Surfaced now so
    # Day 3 threshold calibration is observable from the API, and because it's
    # a column on the PRD's sr_requests table.
    retrieval_confidence: float | None = None
