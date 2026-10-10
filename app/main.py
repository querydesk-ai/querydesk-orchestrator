import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.routers import chat
from app.services.qdrant_store import QdrantStoreError, ensure_collections, ping

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create the Qdrant collections on start so misconfiguration surfaces here
    rather than on a user's first message.

    A Qdrant failure is logged, not raised: the process still starts and
    /health/ready reports the problem. Hard-failing here would mean a
    transient Qdrant blip during a deploy leaves nothing running at all.
    """
    try:
        await ensure_collections()
    except QdrantStoreError as e:
        logger.error("Qdrant not ready at startup: %s", e)
    yield


app = FastAPI(title="QueryDesk Orchestrator", version="0.2.0", lifespan=lifespan)

app.include_router(chat.router)


@app.get("/health")
async def health():
    """Liveness only — no downstream calls, so it stays fast and always answers."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness — actually reaches Qdrant, which the answer path depends on."""
    try:
        await ping()
    except QdrantStoreError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "ready", "qdrant": "ok"}
