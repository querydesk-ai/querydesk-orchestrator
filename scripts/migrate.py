"""
Apply migrations/*.sql to the orchestrator's Postgres (POSTGRES_DSN), in file
name order, each in its own transaction. Applied files are tracked in
schema_migrations, so a second run is a no-op. Plain SQL: no Alembic, no ORM.

An applied migration is never edited: add a new numbered file instead.

Usage:
    uv run scripts/migrate.py            # apply what's pending
    uv run scripts/migrate.py --status   # list applied and pending, change nothing
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(ROOT / "src"))

import asyncpg  # noqa: E402

from querydesk.core.config import settings  # noqa: E402

MIGRATIONS_DIR = ROOT / "migrations"

_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def migrate(status_only: bool) -> int:
    if not settings.postgres_dsn:
        print("POSTGRES_DSN missing from .env")
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    conn = await asyncpg.connect(settings.postgres_dsn)
    try:
        await conn.execute(_TRACKING)
        applied = {
            r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")
        }
        pending = [f for f in files if f.name not in applied]

        for f in files:
            print(f"  {'applied' if f.name in applied else 'pending'}  {f.name}")
        if status_only:
            return 0
        if not pending:
            print("Nothing to apply.")
            return 0

        for f in pending:
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", f.name)
            print(f"Applied {f.name}")
        return 0
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--status", action="store_true", help="list migrations, apply nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(migrate(args.status)))


if __name__ == "__main__":
    main()
