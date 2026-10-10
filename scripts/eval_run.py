"""
Run the labelled eval set against live retrieval and the router. PRD Day 3.

For every row: embed the query once, run hybrid search (top dense score +
cited sources) and category search, and record timings. For in-scope rows it
also asks the LLM tie-break to pick between the top two categories — always,
not only on close calls — so eval_report.py can simulate any tie margin
offline without re-running.

No answers are generated, so a run costs 60 embeddings + up to 50 short LLM
calls. Needs FIREWORKS_API_KEY and QDRANT_* (this is why it's a script, not a
test).

Usage:
    uv run scripts/eval_run.py
    uv run scripts/eval_run.py --no-tie-break    # skip the LLM calls entirely
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querydesk.core.config import BASE_DIR  # noqa: E402
from querydesk.eval.schema import Kind, load_eval_set  # noqa: E402
from querydesk.services.retrieval import qdrant_store  # noqa: E402
from querydesk.services.retrieval.query import embed_query_vectors  # noqa: E402
from querydesk.services.routing.router import tie_break  # noqa: E402

RESULTS_DIR = BASE_DIR / "eval" / "results"


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def run_row(row, with_tie_break: bool) -> dict:
    t = time.perf_counter()
    vectors = await embed_query_vectors(row.query)
    embed_ms = _ms(t)

    t = time.perf_counter()
    hits, top_dense = await qdrant_store.hybrid_search(vectors.dense, vectors.sparse)
    search_ms = _ms(t)

    t = time.perf_counter()
    categories = await qdrant_store.search_categories(vectors.dense, limit=2)
    route_ms = _ms(t)

    llm_choice, tie_break_ms = None, None
    if with_tie_break and row.kind is not Kind.OUT_OF_SCOPE and len(categories) == 2:
        t = time.perf_counter()
        llm_choice = await tie_break(row.query, categories)
        tie_break_ms = _ms(t)

    return {
        **row.model_dump(mode="json"),
        "top_dense_score": top_dense,
        "sources": [h.source_path for h in hits],
        "top_two": [[c.category_id, c.score] for c in categories],
        "llm_choice": llm_choice,
        "latency_ms": {
            "embed": embed_ms,
            "search": search_ms,
            "route": route_ms,
            "tie_break": tie_break_ms,
        },
    }


async def main(with_tie_break: bool) -> Path:
    rows = load_eval_set()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    with out.open("w", encoding="utf-8") as f:
        for i, row in enumerate(rows, start=1):
            result = await run_row(row, with_tie_break)
            f.write(json.dumps(result) + "\n")
            top = result["top_two"][0] if result["top_two"] else ["-", 0.0]
            print(
                f"[{i:2}/{len(rows)}] {row.id:7} dense={result['top_dense_score']:.3f} "
                f"router={top[0]}@{top[1]:.3f}"
            )

    print(f"\nWrote {out}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-tie-break", action="store_true", help="don't call the LLM tie-break")
    args = parser.parse_args()
    asyncio.run(main(with_tie_break=not args.no_tie_break))
