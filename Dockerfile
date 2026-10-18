# QueryDesk orchestrator image. One image runs the api, the Celery worker and
# beat (compose.yaml picks the command).
#
#   docker build --target runtime -t querydesk-orchestrator .
#   docker build --target stand-in -t querydesk-stand-in .   # demo only
#
# The project is installed editable from /app/src, so core/config.py's
# BASE_DIR is /app and data/ resolves the same way it does in a checkout.

# ---- builder: dependencies from uv.lock, plus the BM25 model -----------------
FROM python:3.11-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app

# Dependencies first, in their own layer: a code change doesn't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ src/
RUN uv sync --locked --no-dev

# Bake the BM25 model in, so a container never downloads it at start. The
# local bge model is not baked: embed_backend defaults to fireworks.
RUN .venv/bin/python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding('Qdrant/bm25')"

# ---- runtime: the api / worker / beat image ---------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed

RUN useradd --create-home --uid 10001 querydesk

WORKDIR /app

# Owned by the app user: fastembed may write lock files next to the model.
COPY --from=builder --chown=querydesk /opt/fastembed /opt/fastembed
COPY --from=builder /app/.venv .venv
COPY pyproject.toml ./
COPY src/ src/
COPY data/ data/
COPY migrations/ migrations/
COPY scripts/ scripts/

USER querydesk

EXPOSE 8000

# /health only says the process is up; /health/ready also checks Qdrant.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "querydesk.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

# ---- stand-in: the fake ticketing service, for the demo compose only --------
# Kept out of the runtime image, which carries no test code.
FROM runtime AS stand-in

COPY tests/__init__.py tests/__init__.py
COPY tests/fakes/ tests/fakes/

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/docs', timeout=4)"]

CMD ["uvicorn", "tests.fakes.fake_ticketing:app", "--host", "0.0.0.0", "--port", "8081"]
