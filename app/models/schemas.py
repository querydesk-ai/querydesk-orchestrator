from pydantic import BaseModel


class ChatRequest(BaseModel):
    user_id: str
    query: str


class ChatResponse(BaseModel):
    reply: str
    # resolution_type will become a real enum (RAG / AUTO_SR / MANUAL_ESCALATION)
    # once the decision layer lands in Phase 2.5 — stubbed for now.
    resolution_type: str = "STUB"
