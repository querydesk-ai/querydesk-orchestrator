import logging
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from querydesk.api.routes import chat, health, history, notifications, webhooks, whatsapp
from querydesk.core.errors import UpstreamError, VectorStoreError
from querydesk.services import pipeline
from querydesk.services.retrieval import sparse
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


class _RedactQueryString(logging.Filter):
    """
    Blank the query string in uvicorn's access line. Meta's webhook check puts
    META_VERIFY_TOKEN in it (GET /webhooks/whatsapp?hub.verify_token=…).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and "?" in args[2]:
                args[2] = args[2].split("?", 1)[0] + "?[redacted]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactQueryString())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create the Qdrant collections on start so misconfiguration surfaces here
    rather than on a user's first message.

    A Qdrant failure is logged, not raised: the process still starts and
    /health/ready reports the problem. Hard-failing here would mean a
    transient Qdrant blip during a deploy leaves nothing running at all.

    The BM25 model is loaded here too, so the first /chat after a start doesn't
    spend ~2 s loading it (docs/embedding-trial.md). A failure is logged and the
    model loads lazily on first use instead.
    """
    try:
        await ensure_collections()
    except VectorStoreError as e:
        logger.error("Qdrant not ready at startup: %s", e)
    try:
        await sparse.warm_up()
    except Exception as e:
        logger.error("BM25 model not loaded at startup, will load on first use: %s", e)
    yield
    # Decision rows still being written need the Postgres pool.
    await pipeline.drain_decision_writes()
    await ticketing.aclose()
    await redis_store.aclose()
    await postgres_store.aclose()


app = FastAPI(
    title="QueryDesk Orchestrator",
    version=version("querydesk-orchestrator"),
    lifespan=lifespan,
)


@app.exception_handler(UpstreamError)
async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
    # Any dependency failure (Fireworks, Qdrant, ticketing, Postgres) is a 502:
    # the request was fine, something behind us wasn't. Catching the family means
    # a new dependency needs no change here. The cause goes to the log; the
    # client gets no upstream text.
    logger.error(
        "Upstream failure on %s %s: %s", request.method, request.url.path, exc, exc_info=exc
    )
    return JSONResponse(status_code=502, content={"detail": "A dependency failed"})


app.include_router(health.router)
app.include_router(chat.router)
app.include_router(history.router)
app.include_router(webhooks.router)
app.include_router(whatsapp.router)
app.include_router(notifications.router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")
