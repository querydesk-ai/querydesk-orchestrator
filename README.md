# QueryDesk — Orchestrator

The Python/FastAPI half of **QueryDesk**, an internal AI service desk for an insurance company.
It answers questions the knowledge base covers (citing the source article) and turns action
requests into correctly categorised Service Requests (SRs) it tracks to closure.

Built with a partner who owns the Spring Boot ticketing service (`querydesk-ticketing`); the two
services share nothing but the API contract and `categories.json`. Full spec and the task board:
[docs/task.md](docs/task.md) and the project PRD.

**Current state:** PRD Days 1–4 done.

- **Days 1–2:** hybrid retrieval (dense + BM25) and cited answers.
- **Day 3:** semantic router over the 10 categories, thresholds calibrated on the eval set.
- **Day 4:** the decision layer (a rule engine behind a `DecisionEngine` interface), field
  extraction, and SR creation through the ticketing client with idempotency keys. Eval report v2
  (87 rows): **routing accuracy 97%** (target ≥ 90%), **action requests answered with an article
  0%** (target ≤ 5%) — [docs/eval-report-v2.md](docs/eval-report-v2.md).

**Next:** Day 5 — the TypeSafe Jev engine (waits on a Playground session) and the embedding latency
decision. Slot filling across turns (Redis), decision logs in Postgres and notifications are Days
6–10. The partner's ticketing service doesn't exist yet; a stand-in implements its contract.

## Setup

```bash
# Requires uv: https://docs.astral.sh/uv/
uv sync                          # creates .venv (Python 3.11) from uv.lock, dev tools included

cp .env.example .env
# Fill in FIREWORKS_API_KEY and the QDRANT_* values at minimum.
# TICKETING_BASE_URL defaults to the stand-in below; TYPESAFE_API_KEY is unused until Day 5.
```

`.env` must sit in this directory (next to `.env.example`), not inside `src/`.

## Ingest the knowledge base

The 11 knowledge-base articles live in `data/kb/*.md` (one per category plus a general service-desk
article, written for this project). Each has front matter:

```markdown
---
title: Commission payout schedule
category_id: ops_commission_payout_query
source_url: https://kb.internal/commission
---

Commission for a given month is credited on the 7th of the following month...
```

Then:

```bash
uv run scripts/ingest.py            # chunks, embeds (dense + BM25), upserts to Qdrant
uv run scripts/ingest.py --recreate # only when the collection schema changes
```

Re-running is safe: point ids are deterministic, so chunks update in place instead of duplicating.

## Run

The SR path needs a ticketing service. Until the partner's exists, run the stand-in
(`tests/fakes/fake_ticketing.py`, PRD contract v1.1: idempotency key, 400 on an unknown category,
status transitions) in a second terminal:

```bash
uv run uvicorn tests.fakes.fake_ticketing:app --port 8081
TICKETING_BASE_URL=http://127.0.0.1:8081 uv run uvicorn querydesk.main:app --reload
```

```bash
curl http://127.0.0.1:8000/health          # liveness
curl http://127.0.0.1:8000/health/ready    # also pings Qdrant

curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test","query":"when is my commission credited?"}'
```

Each message gets exactly one `decision` from the rule engine (first match wins):

| Decision | `resolution_type` | When |
| --- | --- | --- |
| `STATUS_LOOKUP` | `MANUAL_ESCALATION` | asks about an existing request (lookup itself not built yet) |
| `ANSWER` | `RAG` | not an action, top dense score ≥ `confidence_high` |
| `ANSWER_WITH_DISCLAIMER` | `RAG` | not an action, score ≥ `confidence_mid` |
| `RAISE_SR` | `AUTO_SR` | an action, router confident, all required fields present → `sr_id` |
| `COLLECT_DETAILS` | `AUTO_SR` | as above, but required fields are missing → `missing_fields` |
| `ESCALATE` | `MANUAL_ESCALATION` | anything else |

Config rules in `data/decision_rules.json` apply last and can only tighten a decision (force
approval or escalation). An action request is never answered with an article. Answers carry a
`sources` array and `retrieval_confidence`; a question the knowledge base doesn't cover escalates
with no sources and makes no LLM call at all, by design. Resending the same request returns the
same SR (idempotency key); resending one that's already closed opens a new one.

A 502 mentioning `FIREWORKS_API_KEY` means `.env` isn't being picked up.

## Tests

The suite is fully offline — Fireworks is mocked with `respx`, Qdrant is monkeypatched — so it
needs no API key and no running services.

```bash
uv run pytest -q
uv run pytest tests/unit/test_rag.py -q
uv run pytest tests/unit/test_rag.py::test_no_hits_escalates_without_calling_the_llm -q
uv run ruff check .
uv run ruff format .
```

The suite uses the stand-in ticketing service in-process, so it needs no running services either.

## Eval

The eval set (`eval/eval_set.jsonl`, 87 labelled queries) calibrates the thresholds and measures
the decision layer. It runs against live services:

```bash
uv run scripts/eval_run.py                  # writes eval/results/<timestamp>.jsonl
uv run scripts/eval_report.py               # docs/eval-report-latest.md + threshold proposals
uv run scripts/eval_report.py --out docs/eval-report-vN.md --title "Eval report vN"
uv run --with psutil scripts/latency_spike.py   # local embedding models vs qwen3 (no API key)
```

The report proposes thresholds but never edits config — change them deliberately and record the
decision in the published report.

## Check services

Check that the backing services are reachable (separate from the app, so a failure points at
config rather than application code):

```bash
uv run scripts/check_services.py
```

## Layout

```
src/querydesk/
  main.py                       # FastAPI app + lifespan (ensure_collections at boot)
  core/
    config.py                   # all settings, read from .env — nothing else touches os.environ
    errors.py                   # UpstreamError family → HTTP 502
  api/routes/
    chat.py                     # POST /chat
    health.py                   # /health, /health/ready
  models/schemas.py             # request/response shapes, Decision + ResolutionType
  eval/                         # eval-set schema, threshold calibration, report rendering
  services/
    pipeline.py                 # the whole /chat flow: embed → retrieve + route → decide → act
    rag.py                      # retrieve() + generate() a grounded answer
    catalog.py                  # the only app-code reader of categories.json
    llm/fireworks.py            # the only module that talks to Fireworks
    retrieval/
      query.py                  # embed a message once, shared by retrieval and the router
      qdrant_store.py           # the only module that talks to Qdrant
      sparse.py                 # BM25 sparse vectors (fastembed, CPU)
      chunking.py               # pure text chunking
    routing/router.py           # top-two categories + whether it's confident
    decision/
      signals.py                # rule-based is-action / is-status
      extraction.py             # LLM required-field extraction (SR path only)
      rules.py                  # rule 6 from data/decision_rules.json, tighten-only
      engine.py                 # DecisionEngine interface + RuleEngine
    ticketing/client.py         # the only module that talks to the ticketing service
    state/redis_store.py        # the only module that talks to Redis (query-embedding cache)
tests/
  unit/                         # pure + mocked-dependency tests
  integration/                  # ASGI app driven in-process, still offline
  fakes/fake_ticketing.py       # stand-in ticketing service (PRD contract v1.1)
data/
  categories.json               # the 10 categories — single source of truth
  decision_rules.json           # rule 6: force approval / escalation
  kb/                           # the 11 knowledge-base articles
eval/eval_set.jsonl             # 87 labelled queries
docs/                           # task board, Day 4–5 plan, eval reports
scripts/
  ingest.py                     # build the Qdrant collections
  eval_run.py                   # run the eval set through retrieval, router + decision layer
  eval_report.py                # report + threshold proposals
  latency_spike.py              # local embedding models vs qwen3, in-memory Qdrant
  check_services.py             # Qdrant/Redis/Postgres reachability
```

See [CLAUDE.md](CLAUDE.md) for the architectural invariants — particularly why retrieval confidence
comes from the dense similarity rather than the fused hybrid score.
