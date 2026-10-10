"""
Run this after filling in .env to confirm Qdrant, Redis, and Postgres are
all reachable. Doesn't touch your app code — just the raw connections —
so a failure here points straight at config, not application logic.

Usage:
    pip install qdrant-client redis asyncpg python-dotenv
    python check_services.py
"""

import asyncio
import importlib
import os

from dotenv import load_dotenv

load_dotenv()


def check_qdrant():
    try:
        qdrant_module = importlib.import_module("qdrant_client")
        QdrantClient = qdrant_module.QdrantClient
    except ImportError:
        return False, "qdrant_client is not installed"

    url = os.getenv("QDRANT_URL")
    key = os.getenv("QDRANT_API_KEY")
    if not url or not key:
        return False, "QDRANT_URL or QDRANT_API_KEY missing from .env"
    try:
        client = QdrantClient(url=url, api_key=key)
        collections = client.get_collections()
        return True, f"connected, {len(collections.collections)} existing collection(s)"
    except Exception as e:
        return False, str(e)


def check_redis():
    try:
        redis = importlib.import_module("redis")
    except ImportError:
        return False, "redis is not installed"

    url = os.getenv("REDIS_URL")
    if not url:
        return False, "REDIS_URL missing from .env"
    try:
        r = redis.from_url(url)
        r.ping()
        return True, "connected, PING succeeded"
    except Exception as e:
        return False, str(e)


async def check_postgres():
    try:
        asyncpg = importlib.import_module("asyncpg")
    except ImportError:
        return False, "asyncpg is not installed"

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        return False, "POSTGRES_DSN missing from .env"
    try:
        conn = await asyncpg.connect(dsn)
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
