"""
Liveness and readiness.

Split because they answer different questions: /health says the process is up
(used by a restart policy), /health/ready says it can actually serve a request
(used by a load balancer before sending traffic).
"""

from fastapi import APIRouter, HTTPException

from querydesk.core.errors import VectorStoreError
from querydesk.services.retrieval import qdrant_store

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Liveness only — no downstream calls, so it stays fast and always answers."""
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready():
    """Readiness — actually reaches Qdrant, which the answer path depends on."""
    try:
        await qdrant_store.ping()
    except VectorStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"status": "ready", "qdrant": "ok"}
