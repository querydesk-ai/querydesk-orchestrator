# Embedding trial: qwen3 vs bge-base (2026-10-17)

**Decision: stay on `qwen3-embedding-8b`. `BAAI/bge-base-en-v1.5` is not adopted.** It is 30× faster
to embed, but it fails three of the four adoption criteria from
[latency-recommendation.md](latency-recommendation.md), and no choice of thresholds fixes that. The
`embed_backend` switch stays merged, set to `fireworks`.

## What was run

- `embed_backend = "local"` (`services/llm/local_embed.py`, fastembed on CPU) feeding the whole
  `/chat` decision layer: hybrid search, router, `signals.classify()`, `RuleEngine` and the real LLM
  field extraction.
- Separate collections `kb_chunks_bge768` / `categories_bge768` (768 dims), built with
  `ingest.py --recreate` under env overrides. The qwen3 collections were not touched and are still
  live.
- The same 94-row eval set as the qwen3 run of the same day (`20261017-123713.jsonl`):
  1. bge at the current thresholds (`20261017-152049.jsonl`);
  2. bge at the thresholds `eval_report.py` proposed for it, high 0.66 / mid 0.60 / router_min 0.58
     (`20261017-152352.jsonl`, [eval-report-bge-trial.md](eval-report-bge-trial.md)).
- An offline replay of the real `RuleEngine` over the stored rows, across a grid: high 0.54–0.70,
  mid 0.44–0.62, router_min 0.40–0.62, in 0.01 steps. Each row's recorded extraction was reused.
  The 3 rows with no recorded extraction were counted as correct, so the replay is an upper bound.
  At the current thresholds it gives 87.2%, against the live run's 87%.

## Side by side

| Criterion (adopt only if…) | qwen3, current thresholds | bge, current | bge, calibrated | bge, best on the grid |
| --- | --- | --- | --- | --- |
| Routing accuracy ≥ 95% | **96%** ✅ | 87% ❌ | 81% ❌ | 91.5% ❌ (high ≥ 0.67, mid 0.55–0.56, router_min 0.48) |
| Action requests answered ≤ 5% | 0% ✅ | 0% ✅ | 0% ✅ | 0% ✅ |
| SR-bound category accuracy 100% | **100%** ✅ | 97% ❌ (act-14) | 97%: act-14 escalates instead | 100% (act-14 escalates) |
| Out-of-scope rows escalate | 15/18 | 7/18 ❌ | 17/18 | 11/18 ❌ |
| Embed p50 / p95 | 1849 / 2388 ms | 68 / 149 ms | 68 / 171 ms | — |
| Search p50 / p95 | 238 / 977 ms | 209 / 419 ms | 204 / 313 ms | — |
| Route p50 / p95 | 196 / 341 ms | 185 / 237 ms | 186 / 222 ms | — |

Retrieval quality is identical: informational source hit@k is 100% for both.

## Why bge fails: overlapping score bands

On bge, action requests and out-of-scope messages fall in the same score range, so no single
`router_min_score` can separate them.

- **Out-of-scope messages score high.** On qwen3, every out-of-scope message the router would
  accept scores at most about 0.50. On bge, these clear `router_min_score` 0.46, and the action
  signal sends them down the SR path:
  - oos-02 (0.579), oos-07 (0.560), oos-15 (0.544), oos-17 (0.530) and oos-08 (0.502) collect
    details for an SR;
  - oos-07 would *raise* a password-reset SR.
- **Action requests score low.** The lowest is act-14 at 0.473, and eleven action rows sit below
  0.58.
- **The trade-off can't be escaped.** The calibrated threshold (0.58) escalates those eleven
  genuine requests (routing 81%). Any lower threshold lets out-of-scope messages through. The best
  compromise on the grid is still 3.5 points short.
- **act-14 still goes to the wrong category.** "add a CNAME … in UAT" goes to `it_uat_extension`
  (0.473 vs `it_dns_firewall_change` 0.468), as in the Day 5 spike. At the current thresholds bge
  asks for UAT-extension details, and the conversation would end in an SR in the wrong queue.

The one row where bge is better is inf-24 ("change a DNS record?"), which qwen3 escalates at
dense 0.383. That doesn't offset the rest.

## Per-row differences (current thresholds)

| Row | Expected | qwen3 | bge |
| --- | --- | --- | --- |
| act-14 | RAISE_SR | RAISE_SR `it_dns_firewall_change` | COLLECT_DETAILS `it_uat_extension` ❌ |
| oos-02 | ESCALATE | ESCALATE | COLLECT_DETAILS `it_application_restart` ❌ |
| oos-07 | ESCALATE | ESCALATE | RAISE_SR `it_password_reset` ❌ |
| oos-08 | ESCALATE | ESCALATE | COLLECT_DETAILS `ops_policy_issuance_delay` ❌ |
| oos-09 | ESCALATE | ESCALATE | COLLECT_DETAILS `ops_crm_lead_access` ❌ |
| oos-11 | ESCALATE | ESCALATE | ANSWER_WITH_DISCLAIMER ❌ |
| oos-15 | ESCALATE | ESCALATE | COLLECT_DETAILS `it_application_restart` ❌ |
| oos-16 | ESCALATE | ESCALATE | ANSWER_WITH_DISCLAIMER ❌ |
| oos-17 | ESCALATE | ESCALATE | COLLECT_DETAILS `ops_policy_issuance_delay` ❌ |
| oos-03 | ESCALATE | ANSWER_WITH_DISCLAIMER ❌ | ANSWER ❌ |
| inf-02, inf-19 | ANSWER | ANSWER_WITH_DISCLAIMER | ANSWER |
| inf-24 | ANSWER | ESCALATE ❌ | ANSWER ✅ |

oos-14 and oos-18 fail on both, unchanged.

## The Qdrant search tail (Step 4)

Search latency by row position, over the three runs:

- **Row 1 is always slow** (1.7–2.1 s) on both backends. This is first-request connection setup
  on a fresh client, not a free-tier cold start.
  - In the app, `ensure_collections()` runs in the lifespan hook on the same client, so a user's
    first message doesn't pay it.
  - A keep-warm ping wouldn't help, because the cost is per process, not per idle period.
- **After row 1:**
  - bge runs: p95 is 0.3–0.4 s with no request over 1 s;
  - qwen3 run: four requests between rows 11 and 27 took 1.1–2.5 s, scattered, which looks like
    network jitter. The 4096-dim request body (~80 KB of JSON per search, vs ~15 KB at 768) may
    contribute.
  - That leaves search p95 at about 1 s on qwen3. The Day 5 figure (2.1 s) included the row-1
    connection setup.
- **Recommendation:** no keep-warm job. For Day 10, put the EC2 instance in the same region as the
  Qdrant cluster, and re-measure search p95 from there.

**Also found: BM25 model load on the first query.** Row 1's embed took 2.1 s on bge even after
the dense model was warmed. That cost is the BM25 (`Qdrant/bm25`) sparse model loading on its
first use, and the app pays it on the first `/chat` after every boot, on either backend. A
one-line warm-up of the sparse model in the lifespan hook would remove it. It isn't done in this
branch; it's noted for Day 9.

**On the local backend, the query cache can cost more than it saves.** In the live `/verify` run
on bge, one embed step took 4.1 s with no Redis error logged, so the time went on Upstash round
trips, not the ~40 ms model call. If a local backend is ever adopted, skip the cache for it.

## What this means for the 3 s p95

The qwen3 answer path at p95 is roughly embed 2.4 s + search ~1.0 s + generation ~1.2 s. Even
with search and route overlapping, that's above 3 s for first-time messages. Mitigations:

- the Redis query cache (30–60 ms on a repeated message);
- the BM25 warm-up above.

The miss goes into the Day 9 final eval report as a known limitation.

## The Day 8 threshold proposal for qwen3

Day 8's `eval_report.py` proposed `confidence_mid` 0.49 → 0.56 and `router_min_score` 0.46 → 0.48.
The replay over the qwen3 run gives:

| high / mid / router_min | Routing | Out-of-scope escalated | Action answered | Wrong category |
| --- | --- | --- | --- | --- |
| 0.56 / 0.49 / 0.46 (current) | 95.7% | 15/18 | 0% | 0 |
| 0.56 / 0.56 / 0.48 (proposal) | 94.7% | 17/18 | 0% | 0 |
| 0.56 / 0.49 / 0.48 | 94.7% | 15/18 | 0% | 0 |
| 0.56 / 0.56 / 0.46 | 95.7% | 17/18 | 0% | 0 |

- **`router_min_score` 0.48: rejected.** It costs one action row and gains nothing.
- **`confidence_mid`: kept at 0.49 for now.**
  - Raising it to 0.56 doesn't change accuracy. It swaps two error-code questions (inf-02
    PWD-1407 at 0.495, inf-19 CRM-5503 at 0.511) from a disclaimer answer to escalation. In return,
    two out-of-scope messages (oos-03, oos-14) escalate instead of getting a disclaimer answer.
  - It's a product call: is a lost answer to a real question worse than a disclaimer answer to
    an out-of-scope one? It is left for an explicit decision, not made by default.

## Rollback / re-trial

Everything is set by env, so nothing in `config.py` changed apart from the new defaults-off
settings. To re-run against the trial collections, which are kept:

```bash
EMBED_BACKEND=local EMBED_DIM=768 \
QDRANT_KB_COLLECTION=kb_chunks_bge768 QDRANT_CATEGORIES_COLLECTION=categories_bge768 \
uv run scripts/eval_run.py
```

Revisit when a stronger local model is worth trying. It would need clearly separated
action and out-of-scope score bands before a threshold could work.
