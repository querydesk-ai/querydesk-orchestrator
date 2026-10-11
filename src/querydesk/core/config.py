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
    # score (see qdrant_store.hybrid_search). Calibrated on the 60-query eval
    # set — docs/eval-report-v0.md. Re-run the eval before changing them.
    # high: above every out-of-scope score except one near-miss (0.545).
    confidence_high: float = 0.56
    # mid: below every informational question, including error-code lookups
    # (PWD-1407 scores 0.497) that BM25 retrieves correctly but dense rates low.
    confidence_mid: float = 0.49

    # --- Semantic router (PRD F3) ----------------------------------------
    # Cosine similarity of the query to a category description, calibrated in
    # docs/eval-report-v0.md. Below router_min_score the router isn't
    # confident -> escalate. Re-calibrated on the 83-row set (Day 4): every
    # action request scores >= 0.46, and only informational questions (which
    # rules 2/3 answer without the router) fall below; 1/17 out-of-scope
    # messages ("salary credited", 0.477) still clears it.
    router_min_score: float = 0.46
    # The LLM tie-break is off: on the eval set similarity alone picked 49/50
    # categories, the tie-break alone 44/50 at ~1.4s p50. With a 0.00 margin the
    # top-1 category always stands. Kept behind the flag for re-evaluation.
    router_tie_margin: float = 0.00
    router_tie_break_enabled: bool = False

    # --- Decision layer (PRD Day 4) --------------------------------------
    # Which DecisionEngine runs: "rules" now; "jev" arrives on Day 5.
    decision_engine: str = "rules"

    # --- Ticketing service -------------------------------------------------
    # The partner's Spring Boot service. Until it exists, run the stand-in:
    # uv run uvicorn tests.fakes.fake_ticketing:app --port 8081
    ticketing_base_url: str = ""
    ticketing_timeout_s: float = 10.0

    # --- Content ---------------------------------------------------------
    kb_dir: Path = BASE_DIR / "data" / "kb"
    categories_file: Path = BASE_DIR / "data" / "categories.json"
    # Rule 6 (tighten-only overrides), owned by this repo.
    decision_rules_file: Path = BASE_DIR / "data" / "decision_rules.json"


# Single shared instance — import `settings` elsewhere, don't re-instantiate.
settings = Settings()
