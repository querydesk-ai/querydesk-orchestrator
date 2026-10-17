"""
Ingest the knowledge base and the category catalog into Qdrant. PRD Day 1.

Reads data/kb/*.md (YAML front matter + markdown body) and data/categories.json,
chunks the articles, embeds them densely (the configured embed_backend) and sparsely (BM25), and
upserts both collections.

Point ids are deterministic, so re-running this updates chunks in place rather
than piling up duplicates. Use --recreate only when the collection schema
itself changes.

Usage:
    uv run scripts/ingest.py
    uv run scripts/ingest.py --recreate
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qdrant_client import models  # noqa: E402

from querydesk.core.config import settings  # noqa: E402
from querydesk.services.retrieval import qdrant_store, sparse  # noqa: E402
from querydesk.services.retrieval.chunking import chunk_text  # noqa: E402
from querydesk.services.retrieval.embedding import embed_documents  # noqa: E402


def parse_front_matter(raw: str) -> tuple[dict, str]:
    """
    Split a '---' delimited YAML-ish front matter block from the body.

    Only flat `key: value` pairs are supported, which is all the KB articles
    need — that keeps PyYAML out of the dependency list.
    """
    if not raw.startswith("---"):
        return {}, raw

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw

    meta: dict = {}
    for line in parts[1].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")

    return meta, parts[2].strip()


async def ingest_kb() -> int:
    kb_dir: Path = settings.kb_dir
    if not kb_dir.is_dir():
        print(f"  ! {kb_dir} does not exist — no articles to ingest.")
        return 0

    files = sorted(p for p in kb_dir.glob("*.md"))
    if not files:
        print(f"  ! No .md files in {kb_dir}.")
        return 0

    texts: list[str] = []
    payloads: list[dict] = []
    ids: list[str] = []

    for path in files:
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        chunks = chunk_text(body, settings.chunk_chars, settings.chunk_overlap)
        rel = path.name
        print(f"  {rel}: {len(chunks)} chunk(s)")

        for index, chunk in enumerate(chunks):
            texts.append(chunk)
            ids.append(qdrant_store.point_id(rel, index))
            payloads.append(
                {
                    "text": chunk,
                    "title": meta.get("title", path.stem),
                    "source_path": rel,
                    "source_url": meta.get("source_url"),
                    "category_id": meta.get("category_id"),
                    "chunk_index": index,
                }
            )

    if not texts:
        print("  ! Articles found but no text extracted.")
        return 0

    print(f"  embedding {len(texts)} chunk(s)...")
    dense_vectors = await embed_documents(texts)
    sparse_vectors = await sparse.sparse_embed_documents_async(texts)

    points = [
        models.PointStruct(
            id=ids[i],
            vector={
                qdrant_store.DENSE_VECTOR: dense_vectors[i],
                qdrant_store.SPARSE_VECTOR: sparse_vectors[i],
            },
            payload=payloads[i],
        )
        for i in range(len(texts))
    ]
    await qdrant_store.upsert_kb_chunks(points)
    return len(points)


async def ingest_categories() -> int:
    """
    Embed each category's prose description for the Day 3 semantic router.
    The id doubles as the payload key, so the router gets the category id
    straight back from a hit.
    """
    path: Path = settings.categories_file
    if not path.is_file():
        print(f"  ! {path} not found — skipping categories.")
        return 0

    catalog = json.loads(path.read_text(encoding="utf-8"))
    categories = catalog.get("categories", [])
    if not categories:
        print("  ! categories.json has no categories.")
        return 0

    descriptions = [c["description"] for c in categories]
    vectors = await embed_documents(descriptions)

    points = [
        models.PointStruct(
            id=qdrant_store.point_id("categories.json", i),
            vector={qdrant_store.DENSE_VECTOR: vectors[i]},
            payload={
                "category_id": c["id"],
                "name": c["name"],
                "domain": c["domain"],
                "approval": c["approval"],
                "required_fields": c["required_fields"],
                "default_path": c["default_path"],
                "description": c["description"],
            },
        )
        for i, c in enumerate(categories)
    ]
    await qdrant_store.upsert_categories(points)
    print(f"  {len(points)} category/categories embedded")
    return len(points)


async def main(recreate: bool) -> None:
    print(
        f"Backend {settings.embed_backend} ({settings.embed_model_id}, "
        f"{settings.embed_dim} dims) -> {settings.qdrant_kb_collection}, "
        f"{settings.qdrant_categories_collection}"
    )
    print(f"Ensuring collections (recreate={recreate})...")
    await qdrant_store.ensure_collections(recreate=recreate)

    print("\nIngesting knowledge base:")
    chunk_count = await ingest_kb()

    print("\nIngesting categories:")
    category_count = await ingest_categories()

    print(f"\nDone. {chunk_count} KB chunk(s), {category_count} category/categories.")
    if chunk_count == 0:
        print(
            "No KB chunks were indexed. Add articles to data/kb/*.md with front "
            "matter (title, category_id, source_url) before expecting /chat to "
            "answer anything."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest the QueryDesk knowledge base.")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and rebuild both collections. Needed only on a schema change.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.recreate))
