"""
Compare Jev with the rule engine's signals on the eval set (PRD Day 5:
"routing accuracy reported for both engines"), spending as few JevStation
credits as possible.

It does not re-run retrieval or routing. It replays the newest full
eval/results/ run (dense score, hits, router top-two, rule signals) and swaps
only the intent signal: the rule engine decides once with signals.classify()'s
recorded intent and once with Jev's. Everything else — thresholds, router,
field extraction, rule 6 — is identical, so any difference is the signal.

Credits:
  - messages are packed `--batch-size` per evaluation (one choice question each;
    JevStation allows 12 questions, and > 5 questions costs 3 credits)
  - every evaluation is cached in eval/results/jev_cache/ by request hash, so a
    re-run spends nothing
  - it stops before an uncached batch would exceed `--budget` credits

Field extraction (Fireworks) runs only for rows Jev sends down the SR path that
the recorded run didn't; the rest reuse the recorded extraction.

Usage:
    uv run scripts/jev_compare.py --max-batches 1      # spend one batch first, check it
    uv run scripts/jev_compare.py --budget 27          # the whole eval set, batches of 10
"""

import argparse
import asyncio
import hashlib
import json
import random
import sys
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querydesk.core.config import BASE_DIR, settings  # noqa: E402
from querydesk.eval.report import _decisions, _pct  # noqa: E402
from querydesk.services import catalog  # noqa: E402
from querydesk.services.decision.engine import DecisionInput, RuleEngine  # noqa: E402
from querydesk.services.decision.extraction import Extraction, extract_fields  # noqa: E402
from querydesk.services.decision.signals import Intent  # noqa: E402
from querydesk.services.llm import jev  # noqa: E402
from querydesk.services.retrieval.qdrant_store import CategoryHit  # noqa: E402
from querydesk.services.routing.router import RouterResult  # noqa: E402

RESULTS_DIR = BASE_DIR / "eval" / "results"
CACHE_DIR = RESULTS_DIR / "jev_cache"
# Already classified on its own (eval/results/jev-probe-01.json): putting it in
# batch 1 shows whether batching changes Jev's answer.
PROBED_ROW = "act-10"


def batch_order(rows: list[dict]) -> list[dict]:
    """
    A fixed, mixed order. The eval set is grouped by kind, so unshuffled batches
    would be ten action requests at a time — a weak check, and a batch of
    near-identical messages could bias the answers. Seeded, so the batches (and
    therefore the cache keys) never change.
    """
    shuffled = sorted(rows, key=lambda r: r["id"])
    random.Random(0).shuffle(shuffled)
    shuffled.sort(key=lambda r: r["id"] != PROBED_ROW)  # stable: probed row first
    return shuffled


def latest_full_run() -> tuple[Path, list[dict]]:
    from querydesk.eval.schema import load_eval_set

    n = len(load_eval_set())
    for path in sorted(RESULTS_DIR.glob("2*.jsonl"), reverse=True):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if len(rows) == n and all("intent" in r for r in rows):
            return path, rows
    raise SystemExit("No full eval run with decision-layer output; run scripts/eval_run.py first.")


def _cost(request: dict) -> int:
    """JevStation's price: 1 credit, or 3 past 5 questions or 8,000 chars of state."""
    big = len(request["questions"]) > 5 or len(json.dumps(request["state"])) > 8000
    return 3 if big else 1


def _cache_path(request: dict) -> Path:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.json"


async def classify_rows(rows: list[dict], batch_size: int, budget: int, max_batches: int | None):
    """Jev intent per row id, from cache where possible. Stops at the budget."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    intents: dict[str, jev.JevIntent] = {}
    spent, remaining = 0, None
    ordered = batch_order(rows)
    batches = [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]
    if max_batches is not None:
        batches = batches[:max_batches]

    for n, batch in enumerate(batches, start=1):
        request = jev.build_intent_request([r["query"] for r in batch])
        path = _cache_path(request)
        if path.exists():
            evaluation = jev.Evaluation(**json.loads(path.read_text(encoding="utf-8")))
            source = "cache"
        else:
            if spent + _cost(request) > budget:
                print(f"  stop: batch {n} would cost {_cost(request)}, {spent}/{budget} spent")
                break
            evaluation = await jev.evaluate(request)
            path.write_text(json.dumps(evaluation.__dict__, indent=2), encoding="utf-8")
            spent += evaluation.credits_charged or _cost(request)
            remaining = evaluation.credits_remaining
            source = f"charged {evaluation.credits_charged}, {evaluation.latency_ms} ms"
        for row, intent in zip(batch, jev.parse_intents(request, evaluation), strict=True):
            intents[row["id"]] = intent
        print(f"  batch {n}/{len(batches)} ({len(batch)} msgs): {source}")

    print(f"  credits spent this run: {spent}" + (f", remaining {remaining}" if remaining else ""))
    return intents


def _router(row: dict) -> RouterResult:
    """The router's verdict, rebuilt from the recorded top-two (same rule as route())."""
    hits = [CategoryHit(cid, "", "", score) for cid, score in row["top_two"]]
    result = RouterResult(top_two=hits)
    top = hits[0] if hits else None
    if (
        top
        and top.score >= settings.router_min_score
        and top.category_id in catalog.all_categories()
    ):
        result.category_id, result.confident = top.category_id, True
    return result


def make_extractor(rows_by_query: dict[str, dict]):
    """Reuse the recorded extraction when the run extracted for this category; else call it."""
    memo: dict[tuple[str, str], Extraction] = {}

    async def extractor(query: str, category) -> Extraction:
        key = (query, category.id)
        if key not in memo:
            row = rows_by_query[query]
            if row.get("decided_category") == category.id and row.get("extraction_ok") is not None:
                memo[key] = Extraction(
                    fields=row["fields"], missing=row["missing_fields"], ok=row["extraction_ok"]
                )
            else:
                memo[key] = await extract_fields(query, category)
        return memo[key]

    return extractor


async def decide_all(rows: list[dict], intents: dict[str, jev.JevIntent]):
    engine = RuleEngine(extractor=make_extractor({r["query"]: r for r in rows}))
    rules_rows, jev_rows = [], []
    for r in rows:
        if r["id"] not in intents:
            continue
        for intent, out_rows in (
            (Intent(**r["intent"]), rules_rows),
            (intents[r["id"]].as_intent(), jev_rows),
        ):
            out = await engine.decide(
                DecisionInput(
                    query=r["query"],
                    top_dense_score=r["top_dense_score"],
                    has_hits=bool(r["sources"]),
                    router=_router(r),
                    intent=intent,
                )
            )
            out_rows.append({**r, "decision": out.decision.value, "rule": out.rule})
    return rules_rows, jev_rows


def render(run: Path, rules_rows: list[dict], jev_rows: list[dict], intents) -> str:
    rules, jv = _decisions(rules_rows), _decisions(jev_rows)
    lines = [
        f"Replayed `{run.name}`, {rules['n']} rows. Same thresholds, router and extraction; "
        "only the intent signal differs.",
        "",
        "| Metric | Rule signals | Jev |",
        "| --- | --- | --- |",
        f"| Routing accuracy (lenient) | {_pct(rules['lenient'])} | {_pct(jv['lenient'])} |",
        f"| Routing accuracy (strict) | {_pct(rules['strict'])} | {_pct(jv['strict'])} |",
    ]
    for kind in rules["by_kind"]:
        lines.append(
            f"| — {kind} | {_pct(rules['by_kind'][kind])} | {_pct(jv['by_kind'].get(kind))} |"
        )
    lines.append(
        f"| Action requests answered with an article | {_pct(rules['action_answered'])} | "
        f"{_pct(jv['action_answered'])} |"
    )
    lines += ["", "Rows where the engines disagree:", ""]
    lines += [
        "| Row | Query | Expected | Rules | Jev (label, conf) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a, b in zip(rules_rows, jev_rows, strict=True):
        if a["decision"] != b["decision"]:
            ji = intents[a["id"]]
            conf = f"{ji.confidence:.2f}" if ji.confidence is not None else "—"
            lines.append(
                f"| {a['id']} | {a['query'][:70]} | {a['expected_decision']} | "
                f"{a['decision']} | {b['decision']} ({ji.label}, {conf}) |"
            )
    return "\n".join(lines)


async def main(batch_size: int, budget: int, max_batches: int | None) -> None:
    run, rows = latest_full_run()
    print(f"Replaying {run.name} ({len(rows)} rows), batches of {batch_size}, budget {budget}")
    intents = await classify_rows(rows, batch_size, budget, max_batches)
    if not intents:
        print("No rows classified (budget too small and nothing cached).")
        return
    rules_rows, jev_rows = await decide_all(rows, intents)

    recorded = {r["id"]: r["decision"] for r in rows}
    drift = sum(a["decision"] != recorded[a["id"]] for a in rules_rows)
    if drift:
        print(
            f"  note: {drift} rule-engine decisions differ from the recorded run (config changed?)"
        )
    print()
    print(render(run, rules_rows, jev_rows, intents))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help=f"messages per evaluation (max {jev.MAX_QUESTIONS})",
    )
    parser.add_argument("--budget", type=int, default=3, help="max credits to spend this run")
    parser.add_argument("--max-batches", type=int, default=None, help="only the first N batches")
    args = parser.parse_args()
    asyncio.run(main(args.batch_size, args.budget, args.max_batches))
