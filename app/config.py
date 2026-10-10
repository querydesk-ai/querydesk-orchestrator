"""
Central app configuration. Every external dependency (Fireworks, Jev, Qdrant,
Postgres, Redis) reads its settings from here — nothing reaches into
os.environ directly elsewhere in the codebase. Makes it trivial to see
everything this app depends on in one place, and to swap values in tests.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fireworks (RAG generation + embeddings)
    fireworks_api_key: str = ""
    fireworks_chat_model: str = "accounts/fireworks/models/llama-v3p1-70b-instruct"
    fireworks_embed_model: str = "accounts/fireworks/models/nomic-embed-text-v1.5"

    # TypeSafe Jev (decision layer)
    typesafe_api_key: str = ""

    # Infra — filled in as you provision Phase 0 services
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    postgres_dsn: str = ""
    redis_url: str = ""


# Single shared instance — import `settings` elsewhere, don't re-instantiate.
settings = Settings()
