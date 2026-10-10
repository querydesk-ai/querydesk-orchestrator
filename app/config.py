"""
Central app configuration. Every external dependency (Fireworks, Jev, Qdrant,
Postgres, Redis) reads its settings from here — nothing reaches into
os.environ directly elsewhere in the codebase. Makes it trivial to see
everything this app depends on in one place, and to swap values in tests.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (this file is app/config.py), used to resolve bundled data files.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fireworks (RAG generation + embeddings)
    fireworks_api_key: str = ""
    fireworks_chat_model: str = "accounts/fireworks/models/llama-v3p1-70b-instruct"
    fireworks_embed_model: str = "accounts/fireworks/models/nomic-embed-text-v1.5"

    # TypeSafe Jev (decision layer) — unused until PRD Day 4; the rule engine
    # ships first behind the same interface so Jev can't block the critical path.
    typesafe_api_key: str = ""

    # Infra
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    postgres_dsn: str = ""
    redis_url: str = ""

    # --- Retrieval -------------------------------------------------------
    qdrant_kb_collection: str = "kb_chunks"
    qdrant_categories_collection: str = "categories"

    # nomic-embed-text-v1.5 emits 768 dims. ensure_collections() verifies the
    # live model against this rather than trusting it, so swapping
    # fireworks_embed_model can't silently create a mismatched collection.
    embed_dim: int = 768

    # Chunks handed to the LLM as context.
    retrieval_top_k: int = 5
    # Candidates pulled per branch (dense, sparse) before RRF fusion. Wider
    # than top_k so fusion has something to actually rerank.
    retrieval_prefetch_limit: int = 20

    chunk_chars: int = 1200
    chunk_overlap: int = 150
    embed_batch_size: int = 32

    # --- Decision thresholds ---------------------------------------------
    # Compared against the top *dense* cosine similarity, never the fused RRF
    # score (see qdrant_store.hybrid_search). Placeholders until the 60-query
    # eval set calibrates them on PRD Day 3 — nothing branches on these yet.
    confidence_high: float = 0.75
    confidence_mid: float = 0.55

    # --- Content ---------------------------------------------------------
    kb_dir: Path = BASE_DIR / "data" / "kb"
    categories_file: Path = BASE_DIR / "data" / "categories.json"


# Single shared instance — import `settings` elsewhere, don't re-instantiate.
settings = Settings()
