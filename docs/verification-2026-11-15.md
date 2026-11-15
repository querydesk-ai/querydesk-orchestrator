# Verification, 2026-11-15

A full re-check of `main` at `668df92`, which includes small talk, SR confirmation and chat history
(#27). Models and thresholds are unchanged since [eval-report-v3.md](eval-report-v3.md):
qwen3-embedding-8b + minimax-m3, rule engine, high 0.56 / mid 0.49 / router_min 0.46. The run used
the live Fireworks, Qdrant Cloud, Upstash and Neon services from a laptop on a home connection,
against the stand-in ticketing service.

## Offline checks

| Check | Result |
| --- | --- |
| `uv run pytest -q` | 601 passed |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed |

## Eval set, 3 runs

`uv run scripts/eval_run.py` three times (94 rows each, query cache off). Runs
`20261115-173210`, `20261115-173614`, `20261115-174015`.

| Metric | Run 1 | Run 2 | Run 3 | PRD target |
| --- | --- | --- | --- | --- |
| Routing accuracy | 96% | 96% | 96% | ≥ 90% ✅ |
| Routing accuracy, strict | 94% | 94% | 94% | — |
| Category accuracy, SR-bound (36) | 100% | 100% | 100% | ≥ 90% ✅ |
| Category top-1, all in-scope (70) | 99% | 99% | 99% | — |
| Action requests answered with an article | 0% | 0% | 0% | ≤ 5% ✅ |

The same six rows miss in every run, the known misses listed in v3: inf-02, inf-19, inf-24,
oos-03, oos-14, oos-18.

Stage latency, pooled over the 3 runs (nearest-rank percentiles, as in the reports):

| Stage | n | p50 | p95 |
| --- | --- | --- | --- |
| Embed (Fireworks qwen3) | 282 | 1,400 ms | 2,065 ms |
| Search | 282 | 254 ms | 438 ms |
| Route | 282 | 221 ms | 384 ms |
| Field extraction (SR path) | 111 | 1,408 ms | 2,011 ms |

## Answer path end to end, 3 runs

The same 20 informational questions as v3 (the first two by id per category), sent through
`pipeline.handle_message()`, the code behind `POST /chat`. Timings come from each message's
`querydesk.decision` record. The query cache was off, so every message paid the full embed.
All 60 messages were answered (18 `ANSWER`, 2 `ANSWER_WITH_DISCLAIMER` per run).

| | Run 1 | Run 2 | Run 3 | Pooled (60) |
| --- | --- | --- | --- | --- |
| p50 | 3.58 s | 3.38 s | 3.53 s | **3.55 s** |
| p95 | 4.15 s | 4.28 s | 4.71 s | **4.36 s** ❌ (< 3 s) |

| Stage (pooled) | p50 | p95 |
| --- | --- | --- |
| Embed (qwen3 + BM25) | 1,460 ms | 1,883 ms |
| Search + route (concurrent) | 366 ms | 1,063 ms |
| Generate (minimax-m3) | 1,650 ms | 2,289 ms |

v3 measured 5.8 s p50 / 7.9 s p95 on 2026-10-17 with the same code path and models. The
difference is the network and provider response times on the day, not a code change. Both are
single-laptop measurements; the target is missed in both.

Not re-measured here: answer support (18 of 20, a human review) and notification timing over 10
SRs (hand-timed). Their v3 figures stand.

## Live flows

App, stand-in ticketing and Celery worker running locally (the `verify` skill), driven over
HTTP and in the chat page. 31 of 31 scripted checks passed.

| Flow | Result |
| --- | --- |
| Small talk ("hi", "what can you do?", "thanks!") | `SMALLTALK`, fixed reply, no search |
| Question the KB covers | `ANSWER` with citations to the source article |
| Out-of-scope question (weather) | `ESCALATE`, recorded in `sr_requests` |
| Request with all fields | `CONFIRM_SR` → "yes" → `RAISE_SR` with an `sr_id` |
| Same request again | `RAISE_SR` with the existing `sr_id` ("You already have SR … open") |
| Request missing fields | `COLLECT_DETAILS` → fields over two replies → `CONFIRM_SR` |
| Small talk during an open request | answered, and the reply reminds the user what is still needed |
| Correction at the confirm step ("actually make it UAT-3") | `CONFIRM_SR` again with the new value |
| "no" at the confirm step | `CANCELLED`, nothing raised; a later "yes" is just small talk |
| "cancel" with nothing open / inside a request | "nothing to cancel" / `CANCELLED` |
| "status of my requests" / someone else's SR | lists the open SR, no embedding or search / "can't find" |
| `GET /history` | returns the exchanges; the chat page restores them after a user id switch |
| Approval SR → `APPROVED` on the stand-in | signed webhook → worker (`written=True`) → notification in 6.5 s → marked read |
| Webhook replayed / wrong signature | 202 with `written=False` / 401 |
| Chat page | status change shows as a notification bubble within 10 s |
| Neon | `sr_requests`, `notifications` and `decision_logs` rows match the flows |

## Finding: confirmation needed Redis (fixed)

With Redis unreachable (`REDIS_URL=redis://127.0.0.1:1`) nothing returns a 500: small talk,
answers and status questions work, and `GET /history` returns `[]`. But a complete request got
`CONFIRM_SR` ("Reply yes to raise it"), the pending SR couldn't be saved, and the user's "yes"
then arrived with no conversation and was answered as small talk. **No SR was raised and the user
wasn't told.** Before the confirm step, the same message raised the SR in one turn.

**Root cause:** `conversation.save()` swallowed the Redis error and returned nothing, so
`_propose_sr()` sent the confirm prompt whether or not the pending SR was saved.

**Fix:** `save()` now returns whether it saved. When it can't, `_propose_sr()` escalates instead of
asking. An SR is still never raised without a "yes". The escalation is recorded in `sr_requests`
with the category and fields (override `unsaved: …`), and the reply says it went to the desk. If
Postgres is down too, the reply asks the user to try again or contact the desk.

**Verified:** two new integration tests fail without the fix and pass with it (603 passed). Live,
with Redis unreachable, the UAT request returned `ESCALATE` with the new reply, the stand-in had no
SR, and Neon had an `ESCALATED` row with all three fields. With Redis up, confirm → "yes" → SR was
unchanged.
