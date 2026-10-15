"""
Run this after filling in .env to confirm Qdrant, Redis, and Postgres are
all reachable. Settings come from core/config.py like everywhere else, but the
connections are raw clients rather than the app's service modules — so a
failure here points straight at config, not application logic.

Usage:
    uv run scripts/check_services.py
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import asyncpg  # noqa: E402
import redis  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

from querydesk.core.config import settings  # noqa: E402


def check_qdrant():
    if not settings.qdrant_url or not settings.qdrant_api_key:
        return False, "QDRANT_URL or QDRANT_API_KEY missing from .env"
    try:
        client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        collections = client.get_collections()
        return True, f"connected, {len(collections.collections)} existing collection(s)"
    except Exception as e:
        return False, str(e)


def check_redis():
    if not settings.redis_url:
        return False, "REDIS_URL missing from .env"
    try:
        r = redis.from_url(settings.redis_url)
        r.ping()
        return True, "connected, PING succeeded"
    except Exception as e:
        return False, str(e)


async def check_postgres():
    if not settings.postgres_dsn:
        return False, "POSTGRES_DSN missing from .env"
    try:
        conn = await asyncpg.connect(settings.postgres_dsn)
        version = await conn.fetchval("SELECT version()")
        await conn.close()
        return True, f"connected, {version.split(',')[0]}"
    except Exception as e:
        return False, str(e)


def main():
    print("Checking service connections...\n")

    for name, fn in [("Qdrant", check_qdrant), ("Redis", check_redis)]:
        ok, msg = fn()
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {name}: {msg}")

    ok, msg = asyncio.run(check_postgres())
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] Postgres: {msg}")


if __name__ == "__main__":
    main()
