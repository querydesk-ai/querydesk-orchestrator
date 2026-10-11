import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from querydesk.api.routes import chat, health
from querydesk.core.errors import VectorStoreError
from querydesk.services.retrieval.qdrant_store import ensure_collections
from querydesk.services.ticketing import client as ticketing

logger = logging.getLogger(__name__)

# uvicorn configures only its own loggers; without this the app's INFO lines —
# including one structured decision per message (querydesk.decision) — vanish.
_app_log = logging.getLogger("querydesk")
if not _app_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    _app_log.addHandler(_handler)
    _app_log.setLevel(logging.INFO)


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
    except VectorStoreError as e:
        logger.error("Qdrant not ready at startup: %s", e)
    yield
    await ticketing.aclose()


app = FastAPI(
    title="QueryDesk Orchestrator",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(chat.router)
