# Embedding latency: recommendation (Day 5)

> **Outcome (2026-10-17):** the bge-base trial ran through the full decision layer and **failed**:
> routing at best 91.5% at any thresholds, out-of-scope messages pass the router, act-14 still goes
> to `it_uat_extension`. Staying on qwen3 + the cache. Search-tail finding and numbers in
> [embedding-trial.md](embedding-trial.md).

**Decision: keep `qwen3-embedding-8b` with the Redis query cache for now. `BAAI/bge-base-en-v1.5`
is the only local model worth a trial, and it must pass a full decision-layer eval before it
replaces qwen3.** Decide before Day 8.

## The problem

PRD target: answer path p95 < 3 s. From the v2 eval run (`eval/results/20261011-194607.jsonl`,
87 rows):

| Stage | p50 | p95 |
| --- | --- | --- |
| Dense embed (Fireworks, qwen3) | 2241 ms | 3618 ms |
| Hybrid search (Qdrant Cloud) | 416 ms | 2108 ms |
| Route (runs alongside search) | 275 ms | 587 ms |
| Generation (minimax-m3, measured Day 2) | ~1200 ms | — |

So the answer path sits around **3.8 s p50**. The embed step alone uses up the 3 s budget at p95.

## What shipped: the query cache

Dense query vectors are cached in Redis (`qemb:{model}:{sha256}`, 7-day TTL). Live result: 2.6 s on
the first call, then 30–60 ms for a repeat. This only helps repeated messages. First-time messages
still pay the full embed cost, so the cache alone can't meet the target.

## The spike: local CPU models

Run with `uv run --with psutil scripts/latency_spike.py`. It uses an in-memory Qdrant, the same
chunking, the same BM25 + RRF hybrid search and the same category descriptions as production. The
live collections and thresholds were not touched.

| Model | Dim | hit@1 | Router top-1 (all / actions) | Action min / OOS max | Embed p50 / p95 | RSS |
| --- | --- | --- | --- | --- | --- | --- |
| qwen3-embedding-8b (Fireworks) | 4096 | 100% | 99% / **100%** | 0.478 / 0.478 | 2241 / 3618 ms | — |
| bge-small-en-v1.5 | 384 | 100% | 89% / 89% | 0.544 / 0.724 | 11 / 16 ms | ~130 MB |
| **bge-base-en-v1.5** | 768 | 100% | 94% / **97%** | 0.473 / 0.621 | 36 / 53 ms | ~380 MB |
| nomic-embed-text-v1.5 | 768 | 100% | 90% / 89% | 0.572 / 0.642 | 46 / 69 ms | ~530 MB |

34 informational rows (hit@1), 70 rows with a category, 36 action rows. Cold start is about 2 s per
model once downloaded.

What the numbers say:

- **Retrieval is not the problem.** Every model gets 100% hit@1, because BM25 in the hybrid
  search carries the exact-token rows.
- **Routing is.** qwen3 routes every action request correctly. The local models route action
  requests wrongly, and always the same way: infrastructure requests get pulled toward
  `it_uat_extension`.
  - bge-small: act-08, act-13, act-14, act-31
  - bge-base: act-14 only
  - nomic: act-08, act-13, act-14, act-31

  A wrong category opens an SR in the wrong queue, which is worse than escalating.
- **Separability gets worse.** For bge-base, some out-of-scope messages score 0.621, well above
  the lowest action request (0.473). No single `router_min_score` can then tell them apart, so
  rejecting out-of-scope "action-looking" messages would rest entirely on `signals.classify()`.
  For qwen3 the two only touch (0.478 vs 0.478). That is the known "salary credited" near-miss.

## Recommendation

1. **Now:** keep qwen3 plus the cache. Routing quality is the PRD's most important property, and
   qwen3 is the only model at 100% on action requests.
2. **Trial bge-base before Day 8** (about half a day). It is the only candidate within a point of
   the routing target, and at about 380 MB it fits on the t3.medium next to the JVM. The trial:
   - an `embed_backend` setting (`fireworks` | `local`) inside `services/llm/` or a sibling owner
     module, keeping `embed_documents()` / `embed_query()` as the only entry points
   - separate collections (e.g. `kb_chunks_bge768`, `categories_bge768`) and
     `ingest.py --recreate` into them, so qwen3 stays live
   - `eval_run.py` + `eval_report.py` through the **whole decision layer**, then re-calibrate
     `confidence_high` / `confidence_mid` / `router_min_score` (invariant #4)
3. **Adopt bge-base only if** the trial eval shows:
   - routing accuracy ≥ 95%
   - action requests answered with an article ≤ 5%
   - no SR raised in the wrong category
   - out-of-scope rows still escalate

   Otherwise stay on qwen3. Record the p95 miss in the final eval report, with the cache as the
   mitigation.
4. **Either way, look at Qdrant search p95 (2.1 s).** Even with a 50 ms local embed, search p95
   plus generation leaves little headroom under 3 s. Check whether that tail is free-tier cold
   starts or network before Day 8.

Don't tune the category descriptions to fix the UAT pull using this eval set. That would overfit
the very rows that measure it. If descriptions change, add new held-out action rows first.
