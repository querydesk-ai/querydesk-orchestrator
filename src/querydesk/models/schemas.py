from enum import StrEnum

from pydantic import BaseModel, Field


class ResolutionType(StrEnum):
    """
    How a message was resolved (the sr_requests.resolution_type column).

    Only add a value once the code can actually emit it, so the API never
    advertises an outcome that can't happen.
    """

    RAG = "RAG"
    # The SR path: an SR was raised, or details are being collected for one.
    AUTO_SR = "AUTO_SR"
    MANUAL_ESCALATION = "MANUAL_ESCALATION"


class Decision(StrEnum):
    """The decision layer's outcomes (PRD rules 1–6); one per message."""

    ANSWER = "ANSWER"
    ANSWER_WITH_DISCLAIMER = "ANSWER_WITH_DISCLAIMER"
    COLLECT_DETAILS = "COLLECT_DETAILS"
    RAISE_SR = "RAISE_SR"
    ESCALATE = "ESCALATE"
    STATUS_LOOKUP = "STATUS_LOOKUP"
    # Not a rule outcome: the user cancelled an open slot-filling conversation.
    CANCELLED = "CANCELLED"


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
    # Top dense cosine similarity, never a fused RRF score. Surfaced so
    # threshold calibration is observable from the API, and because it's a
    # column on the PRD's sr_requests table.
    retrieval_confidence: float | None = None
    # What the decision layer chose, and what it acted on. All optional so a
    # client built against the Day 2 response keeps working.
    decision: Decision | None = None
    category_id: str | None = None
    sr_id: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    requires_approval: bool | None = None


class SRStatusEvent(BaseModel):
    """
    The ticketing service's status webhook body (PRD contract v1.1,
    POST {orchestrator}/webhooks/sr-status). Only `sr_id`, `user_id` and
    `to_status` drive the notification; the rest is carried for the log.
    """

    sr_id: str
    user_id: str
    from_status: str | None = None
    to_status: str
    note: str | None = None
    changed_at: str | None = None
