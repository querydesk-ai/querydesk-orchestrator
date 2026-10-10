"""
Central app configuration. Every external dependency (Fireworks, Jev, Qdrant,
Postgres, Redis) reads its settings from here — nothing reaches into
os.environ directly elsewhere in the codebase. Makes it trivial to see
everything this app depends on in one place, and to swap values in tests.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: this file is src/querydesk/core/config.py, so up four levels.
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Fireworks -------------------------------------------------------
    # Both models the PRD originally named (nomic-embed-text-v1.5 and
    # llama-v3p1-70b-instruct) are no longer served on Fireworks. These are
    # the chosen replacements from what the account actually offers:
    #   qwen3-embedding-8b — the only embedding model available, 4096 dims
    #   minimax-m3         — fastest of eight benchmarked (~1.2s on a RAG
    #                        prompt), which matters for the PRD's 3s p95
    fireworks_api_key: str = ""
    fireworks_chat_model: str = "accounts/fireworks/models/minimax-m3"
    fireworks_embed_model: str = "accounts/fireworks/models/qwen3-embedding-8b"

    # TypeSafe Jev (decision layer) — unused until PRD Day 4; the rule engine
    # ships first behind the same interface so Jev can't block the critical path.
    typesafe_api_key: str = ""

    # --- Infra -----------------------------------------------------------
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    postgres_dsn: str = ""
    redis_url: str = ""

    # --- Retrieval -------------------------------------------------------
    qdrant_kb_collection: str = "kb_chunks"
    qdrant_categories_collection: str = "categories"

    # qwen3-embedding-8b emits 4096 dims. ensure_collections() verifies the
    # live model against this rather than trusting it, so changing
    # fireworks_embed_model can't silently create a mismatched collection.
    embed_dim: int = 4096

    # Chunks handed to the LLM as context.
    retrieval_top_k: int = 5
    # Candidates pulled per branch (dense, sparse) before RRF fusion. Wider
    # than top_k so fusion has something to actually rerank.
    retrieval_prefetch_limit: int = 20

    chunk_chars: int = 1200
    chunk_overlap: int = 150
    embed_batch_size: int = 32

    # --- Generation ------------------------------------------------------
    # Every chat model on the account is a reasoning model: hidden reasoning
    # tokens are drawn from the same budget as the answer, so a budget that
    # looks generous for the answer alone can be consumed entirely by
    # reasoning and return empty content. Keep headroom.
    chat_max_tokens: int = 1024
    chat_temperature: float = 0.2

    # HTTP timeouts (seconds). The chat call sits on the synchronous /chat path,
    # so keep it close to the latency budget; embedding is longer because
    # ingestion sends large batches.
    chat_timeout_s: float = 60.0
    embed_timeout_s: float = 120.0

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
