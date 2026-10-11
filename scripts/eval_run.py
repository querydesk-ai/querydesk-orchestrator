"""
Run the labelled eval set against live retrieval, the router and the decision
layer.

For every row: embed the query once, run hybrid search (top dense score +
cited sources), route it, classify it and run the configured decision engine —
including the real LLM field extraction on the SR path. Timings are recorded
per stage. No answers are generated and no SRs are created, so a run costs one
embedding per row plus one short LLM call per SR-bound row.

With --tie-break it also asks the LLM tie-break to pick between the top two
categories for every in-scope row, so eval_report.py can simulate any tie
margin offline.

Needs FIREWORKS_API_KEY and QDRANT_* (this is why it's a script, not a test).

Usage:
    uv run scripts/eval_run.py
    uv run scripts/eval_run.py --tie-break      # also record LLM tie-break picks
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querydesk.core.config import BASE_DIR  # noqa: E402
from querydesk.eval.schema import Kind, load_eval_set  # noqa: E402
from querydesk.services.decision.engine import DecisionInput, get_engine  # noqa: E402
from querydesk.services.decision.signals import classify  # noqa: E402
from querydesk.services.retrieval import qdrant_store  # noqa: E402
from querydesk.services.retrieval.query import embed_query_vectors  # noqa: E402
from querydesk.services.routing.router import route, tie_break  # noqa: E402

RESULTS_DIR = BASE_DIR / "eval" / "results"


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def run_row(row, engine, with_tie_break: bool) -> dict:
    t = time.perf_counter()
    vectors = await embed_query_vectors(row.query)
    embed_ms = _ms(t)

    t = time.perf_counter()
    hits, top_dense = await qdrant_store.hybrid_search(vectors.dense, vectors.sparse)
    search_ms = _ms(t)

    t = time.perf_counter()
    router = await route(row.query, vectors.dense)
    route_ms = _ms(t)

    llm_choice, tie_break_ms = None, None
    tie_break_ran = (
        with_tie_break and row.kind is not Kind.OUT_OF_SCOPE and len(router.top_two) == 2
    )
    if tie_break_ran:
        t = time.perf_counter()
        llm_choice = await tie_break(row.query, router.top_two)
        tie_break_ms = _ms(t)

    intent = classify(row.query)
    t = time.perf_counter()
    out = await engine.decide(
        DecisionInput(
            query=row.query,
            top_dense_score=top_dense,
            has_hits=bool(hits),
            router=router,
            intent=intent,
        )
    )
    decide_total_ms = _ms(t)

    return {
        **row.model_dump(mode="json"),
        "top_dense_score": top_dense,
        "sources": [h.source_path for h in hits],
        "top_two": [[c.category_id, c.score] for c in router.top_two],
        "llm_choice": llm_choice,
        # False when the run skipped the tie-break: llm_choice None is then "not
        # asked", not "the LLM had no answer".
        "tie_break_ran": tie_break_ran,
        "intent": asdict(intent),
        "decision": out.decision.value,
        "rule": out.rule,
        "engine": out.engine,
        "decided_category": out.category_id,
        "fields": out.fields,
        "missing_fields": out.missing_fields,
        "requires_approval": out.requires_approval,
        "overrides": out.overrides,
        "extraction_ok": out.extraction_ok,
        "latency_ms": {
            "embed": embed_ms,
            "search": search_ms,
            "route": route_ms,
            "tie_break": tie_break_ms,
            # The decision layer's own cost, without the LLM extraction call.
            "decide": round(decide_total_ms - (out.extraction_ms or 0), 1),
            "extraction": out.extraction_ms,
        },
    }


async def main(with_tie_break: bool) -> Path:
    rows = load_eval_set()
    engine = get_engine()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    with out.open("w", encoding="utf-8") as f:
        for i, row in enumerate(rows, start=1):
            result = await run_row(row, engine, with_tie_break)
            f.write(json.dumps(result) + "\n")
            mark = "ok " if result["decision"] == row.expected_decision.value else "XX "
            print(
                f"[{i:2}/{len(rows)}] {mark}{row.id:7} {result['decision']:22} "
                f"(want {row.expected_decision.value}) dense={result['top_dense_score']:.3f}"
            )

    print(f"\nWrote {out}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tie-break", action="store_true", help="also record LLM tie-break picks")
    args = parser.parse_args()
    asyncio.run(main(with_tie_break=args.tie_break))
