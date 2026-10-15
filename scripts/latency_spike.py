"""
Latency spike (Day 5): could a local CPU embedding model replace
qwen3-embedding-8b on the /chat path?

For each candidate fastembed model it rebuilds the knowledge base and the
category collection in an *in-memory* Qdrant (the live collections and the
calibrated thresholds are never touched), then runs the eval set through the
same hybrid search as production — dense + BM25 prefetch, RRF fusion — and
the same top-1 category routing. Reported per model:

  - informational hit@1 (fused top source == expected article) and hit@k
  - router top-1 accuracy on rows with a category
  - router top-1 on action requests (the rows rule 4 routes into SRs)
  - router separability: lowest action-request top score vs highest out-of-scope one
  - query embed latency p50/p95 (one query at a time, after a warm-up), cold start

The qwen3 baseline is read from the latest full eval/results/ run.

Needs no API key. Downloads each model once (~70-520 MB) into the fastembed cache.

Usage:
    uv run --with psutil scripts/latency_spike.py          # psutil adds RSS figures
    uv run scripts/latency_spike.py --models BAAI/bge-small-en-v1.5
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastembed import TextEmbedding  # noqa: E402
from qdrant_client import AsyncQdrantClient, models  # noqa: E402

from ingest import parse_front_matter  # noqa: E402
from querydesk.core.config import BASE_DIR, settings  # noqa: E402
from querydesk.eval.schema import Kind, load_eval_set  # noqa: E402
from querydesk.services.retrieval import sparse  # noqa: E402
from querydesk.services.retrieval.chunking import chunk_text  # noqa: E402

RESULTS_DIR = BASE_DIR / "eval" / "results"
KB, CATS = "kb_spike", "categories_spike"


@dataclass(frozen=True)
class Candidate:
    name: str
    query_prefix: str = ""
    doc_prefix: str = ""


# Each model's documented asymmetric prefixes.
CANDIDATES = {
    c.name: c
    for c in [
        Candidate(
            "BAAI/bge-small-en-v1.5",
            query_prefix="Represent this sentence for searching relevant passages: ",
        ),
        Candidate(
            "BAAI/bge-base-en-v1.5",
            query_prefix="Represent this sentence for searching relevant passages: ",
        ),
        Candidate(
            "nomic-ai/nomic-embed-text-v1.5",
            query_prefix="search_query: ",
            doc_prefix="search_document: ",
        ),
    ]
}


def _rss_mb() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / 2**20


def _pct(values: list[float], q: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def load_kb() -> list[dict]:
    chunks = []
    for path in sorted(settings.kb_dir.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        for text in chunk_text(body, settings.chunk_chars, settings.chunk_overlap):
            chunks.append({"text": text, "source_path": path.name})
    return chunks


def load_categories() -> list[dict]:
    return json.loads(settings.categories_file.read_text(encoding="utf-8"))["categories"]


def baseline() -> dict | None:
    """qwen3 numbers from the newest eval run that covers the whole eval set."""
    n = len(load_eval_set())
    for path in sorted(RESULTS_DIR.glob("*.jsonl"), reverse=True):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if len(rows) == n:
            return {"run": path.name, **score(rows)}
    return None


def score(rows: list[dict]) -> dict:
    """Shared scoring for eval-run rows and spike rows (same field names)."""
    info = [r for r in rows if r["kind"] == Kind.INFORMATIONAL]
    routed = [r for r in rows if r["category_id"]]
    oos = [r for r in rows if r["kind"] == Kind.OUT_OF_SCOPE]
    actions = [r for r in rows if r["kind"] == Kind.ACTION]
    embed = [r["latency_ms"]["embed"] for r in rows]
    oos_max = max(r["top_two"][0][1] for r in oos) if oos else None
    return {
        "hit_at_1": sum(r["sources"][:1] == [r["expected_source"]] for r in info) / len(info),
        "hit_at_k": sum(r["expected_source"] in r["sources"] for r in info) / len(info),
        "n_info": len(info),
        "router_top1": sum(r["top_two"][0][0] == r["category_id"] for r in routed) / len(routed),
        "n_routed": len(routed),
        # Rule 4 routes only action requests, so these are the rows that open SRs.
        "router_action_top1": sum(r["top_two"][0][0] == r["category_id"] for r in actions)
        / len(actions),
        "router_action_min": min(r["top_two"][0][1] for r in actions),
        "n_action": len(actions),
        "action_misroutes": [
            (r["id"], r["category_id"], r["top_two"][0][0])
            for r in actions
            if r["top_two"][0][0] != r["category_id"]
        ],
        "router_oos_max": oos_max,
        "embed_p50": statistics.median(embed),
        "embed_p95": _pct(embed, 95),
    }


async def run_candidate(c: Candidate, kb: list[dict], cats: list[dict]) -> dict:
    rss_before = _rss_mb()
    t = time.perf_counter()
    model = TextEmbedding(c.name)
    list(model.embed([c.query_prefix + "warm up"]))
    cold_start_s = time.perf_counter() - t
    rss_after = _rss_mb()

    def embed_docs(texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in model.embed([c.doc_prefix + x for x in texts])]

    kb_dense = embed_docs([k["text"] for k in kb])
    kb_sparse = sparse.sparse_embed_documents([k["text"] for k in kb])
    cat_dense = embed_docs([x["description"] for x in cats])
    dim = len(kb_dense[0])

    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        KB,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    await client.upsert(
        KB,
        points=[
            models.PointStruct(
                id=i,
                vector={"dense": kb_dense[i], "bm25": kb_sparse[i]},
                payload={"source_path": k["source_path"]},
            )
            for i, k in enumerate(kb)
        ],
    )
    await client.create_collection(
        CATS,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
    )
    await client.upsert(
        CATS,
        points=[
            models.PointStruct(id=i, vector={"dense": cat_dense[i]}, payload={"id": x["id"]})
            for i, x in enumerate(cats)
        ],
    )

    rows = []
    for row in load_eval_set():
        t = time.perf_counter()
        dense = next(iter(model.embed([c.query_prefix + row.query]))).tolist()
        embed_ms = (time.perf_counter() - t) * 1000
        sparse_vec = sparse.sparse_embed_query(row.query)

        fused = await client.query_points(
            KB,
            prefetch=[
                models.Prefetch(
                    query=dense, using="dense", limit=settings.retrieval_prefetch_limit
                ),
                models.Prefetch(
                    query=sparse_vec, using="bm25", limit=settings.retrieval_prefetch_limit
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=settings.retrieval_top_k,
            with_payload=True,
        )
        top_cats = await client.query_points(
            CATS, query=dense, using="dense", limit=2, with_payload=True
        )
        rows.append(
            {
                **row.model_dump(mode="json"),
                "sources": [p.payload["source_path"] for p in fused.points],
                "top_two": [[p.payload["id"], p.score] for p in top_cats.points],
                "latency_ms": {"embed": embed_ms},
            }
        )
    await client.close()

    return {
        "model": c.name,
        "dim": dim,
        "cold_start_s": cold_start_s,
        "rss_mb": (rss_after - rss_before) if rss_before is not None else None,
        **score(rows),
    }


def render(base: dict | None, results: list[dict]) -> str:
    def pct(x):
        return f"{x:.0%}"

    def opt(x, fmt):
        return format(x, fmt) if x is not None else "—"

    lines = [
        "| Model | Dim | hit@1 | hit@k | Router top-1 (all / actions) | Action min / OOS max | "
        "Embed p50 | Embed p95 | Cold start | RSS |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if base:
        lines.append(
            f"| qwen3-embedding-8b (Fireworks, `{base['run']}`) | 4096 | {pct(base['hit_at_1'])} | "
            f"{pct(base['hit_at_k'])} | {pct(base['router_top1'])} / "
            f"{pct(base['router_action_top1'])} | "
            f"{base['router_action_min']:.3f} / {opt(base['router_oos_max'], '.3f')} | "
            f"{base['embed_p50']:.0f} ms | {base['embed_p95']:.0f} ms | — | — |"
        )
    for r in results:
        lines.append(
            f"| {r['model']} (local CPU) | {r['dim']} | {pct(r['hit_at_1'])} | "
            f"{pct(r['hit_at_k'])} | {pct(r['router_top1'])} / {pct(r['router_action_top1'])} | "
            f"{r['router_action_min']:.3f} / {opt(r['router_oos_max'], '.3f')} | "
            f"{r['embed_p50']:.0f} ms | {r['embed_p95']:.0f} ms | {r['cold_start_s']:.1f} s | "
            f"{opt(r['rss_mb'], '.0f')} MB |"
        )
    n = results[0] if results else base
    lines.append("")
    lines.append(
        f"hit@1/hit@k over {n['n_info']} informational rows; router top-1 over {n['n_routed']} "
        f"rows with a category / {n['n_action']} action rows. Action min / OOS max: the lowest "
        "top category score of an action request vs the highest of an out-of-scope message "
        "(no overlap = one router_min_score separates them). The baseline's embed latency is "
        "the Fireworks round trip."
    )
    return "\n".join(lines)


async def main(names: list[str]) -> None:
    kb, cats = load_kb(), load_categories()
    print(f"{len(kb)} KB chunks, {len(cats)} categories, {len(load_eval_set())} eval rows\n")
    results = []
    for name in names:
        print(f"Running {name}...")
        results.append(await run_candidate(CANDIDATES[name], kb, cats))
    print()
    print(render(baseline(), results))
    print()
    print("Action requests routed to the wrong category (id: expected -> got):")
    for r in results:
        misses = ", ".join(f"{i}: {want} -> {got}" for i, want, got in r["action_misroutes"])
        print(f"- {r['model']}: {misses or 'none'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", nargs="+", default=list(CANDIDATES), choices=list(CANDIDATES))
    args = parser.parse_args()
    asyncio.run(main(args.models))
