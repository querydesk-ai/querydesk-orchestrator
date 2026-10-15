# Jev notes (Day 5)

**Result: the rule engine stays the default.** On the full 87-row eval set it routes 97% of messages
correctly against Jev's 91%. It never answers an action request with an article (Jev: 3%, one row),
and it decides in ~0 ms against Jev's 3.5–7 s per live call. Jev is wired up as a measured
alternative (`services/llm/jev.py`, `scripts/jev_compare.py`), not used on `/chat`.

## Access: JevStation, not api.typesafe.ai

- The key in `TYPESAFE_API_KEY` is a **JevStation** key (`jevstation.com`, an independent aggregator
  that runs TypeSafe's Jev; it says it is not affiliated with TypeSafe). `api.typesafe.ai` rejects it
  with 401.
- Endpoint: `POST https://jevstation.com/api/v1/systemone`, `Authorization: Bearer <key>`. The body
  follows TypeSafe's System One shape (`model`, `state`, `questions`). The response wraps it:
  `{"code": 0, "data": {"answers", "credits": {"charged", "remaining"}, "latency_ms"}}`.
- **Billing is per evaluation (one request), not per question.**
  - 1 credit for ≤ 5 questions and ≤ 8,000 characters of state; 3 credits beyond either.
  - At most 12 questions per request.
  - Failed runs (e.g. a 400 for a bad body) are free.
- Account: a 50-credit welcome grant, expiring 2026-11-14. **22 credits remain** after this work.

## How we ask

- One `choice` question per message: `action` / `status` / `information` / `out_of_scope`. It covers
  the PRD's "is this an action?" and "is it about an existing request?" in a single question. The
  wording is in `jev.INTENT_CRITERIA`.
- `action` maps to `Intent.is_action`, and `status` maps to `Intent.is_status`.
- The PRD's third question ("does the top article answer it?") was dropped. The dense score already
  plays that role in rules 2–3, and asking it would double the questions per message.
- **Batching:** 10 messages per request, one question each, at 3 credits. All 87 rows took 9
  requests and 27 credits. One message per request would have cost 87.
  - Batching doesn't distort the answers. act-10 ("restart the claims app in UAT") got the same
    answer batched as when asked alone (`eval/results/jev-probe-01.json`).

## Latency

| Call | Latency |
| --- | --- |
| One message, 3 questions (live shape) | 6.7 s round trip; JevStation reports 3.5 s |
| Empty, failed request | 2.3 s (JevStation overhead before the model runs) |
| Batch of 10 messages | 2.7–3.2 s (about 0.3 s per message) |

The decision budget is < 500 ms. Through JevStation, Jev can't sit on the synchronous `/chat`
path. TypeSafe's own docs quote about 0.27 s for a batched call direct, so direct access might
change this. It would need an `api.typesafe.ai` key.

## Comparison (`uv run scripts/jev_compare.py`)

This replays the eval run `20261011-194607.jsonl` and swaps only the intent signal. Thresholds,
router, field extraction and rule 6 are identical. The rule engine's replay matched the recorded
run on every row.

| Metric | Rule signals | Jev |
| --- | --- | --- |
| Routing accuracy (lenient) | **97%** | 91% |
| Routing accuracy (strict) | **94%** | 89% |
| — action | **100%** | 89% |
| — informational | **97%** | 94% |
| — out of scope | 88% | 88% |
| Action requests answered with an article | **0%** | 3% |

Where they disagree:

| Row | Query | Expected | Rules | Jev (label, confidence) |
| --- | --- | --- | --- | --- |
| act-16 | proposal PR-20931 has been under review for 6 days and still not issued | RAISE_SR | RAISE_SR | STATUS_LOOKUP (status, 0.91) |
| act-17 | customer's term policy P-778120 was approved last week but still hasn't… | RAISE_SR | RAISE_SR | STATUS_LOOKUP (status, 0.67) |
| act-18 | my customer's policy hasn't been issued, it's been ages | COLLECT_DETAILS | COLLECT_DETAILS | STATUS_LOOKUP (status, 0.33) |
| act-30 | I can't see my leads | COLLECT_DETAILS | COLLECT_DETAILS | ANSWER (out_of_scope, 0.31) |
| inf-21 | can I restart my own service in UAT | ANSWER | ANSWER | COLLECT_DETAILS (action, 0.47) |

- Three of Jev's five misses are policy-issuance complaints read as "status of a request". The
  boundary between "my SR's status" and "my *policy* is stuck" is exactly where our `status`
  definition is thin.
- Jev's other misses had low confidence (0.31–0.47). A confidence gate wouldn't rescue act-16
  (0.91).
- Re-wording the criteria and re-running would mean tuning against the rows that measure it, and
  would spend the remaining credits. It's not worth it while the rule engine leads.

## If Jev comes back

- Get a direct `api.typesafe.ai` key and re-measure latency. That is the blocker for live use.
- Add **held-out** rows on policy-issuance complaints vs status questions before changing
  `INTENT_CRITERIA`.
- Re-run `scripts/jev_compare.py`. Cached batches are free; changed wording re-spends credits
  (about 27 for the full set).
