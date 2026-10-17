import logging
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from querydesk.api.routes import chat, health, notifications, webhooks
from querydesk.core.errors import VectorStoreError
from querydesk.services.retrieval.qdrant_store import ensure_collections
from querydesk.services.state import postgres_store, redis_store
from querydesk.services.ticketing import client as ticketing

logger = logging.getLogger(__name__)

# The minimal chat page (Day 8): one static file, served same-origin.
INDEX_HTML = Path(__file__).parent / "static" / "index.html"

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
    await redis_store.aclose()
    await postgres_store.aclose()


app = FastAPI(
    title="QueryDesk Orchestrator",
    version=version("querydesk-orchestrator"),
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(webhooks.router)
app.include_router(notifications.router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")
