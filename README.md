# QueryDesk — Orchestrator

The Python/FastAPI half of **QueryDesk**, an internal AI service desk for an insurance company.
It answers questions the knowledge base covers (citing the source article) and — from PRD Day 4 —
turns action requests into correctly categorised Service Requests it tracks to closure.

Built with a partner who owns the Spring Boot ticketing service (`querydesk-ticketing`); the two
services share nothing but the API contract and `categories.json`. Full spec and the task board:
[docs/task.md](docs/task.md) and the project PRD.

**Current state:** PRD Days 1–2 — hybrid retrieval and cited answers. The semantic router,
decision layer, slot filling and notifications are Days 3–10.

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in FIREWORKS_API_KEY and the QDRANT_* values at minimum.
```

`.env` must sit in this directory (next to `.env.example`), not inside `app/`.

## Ingest the knowledge base

Put your KB articles in `data/kb/*.md`, each with front matter:

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
python scripts/ingest.py            # chunks, embeds (dense + BM25), upserts to Qdrant
python scripts/ingest.py --recreate # only when the collection schema changes
```

Re-running is safe: point ids are deterministic, so chunks update in place instead of duplicating.

## Run

```bash
uvicorn app.main:app --reload
```

```bash
curl http://127.0.0.1:8000/health          # liveness
curl http://127.0.0.1:8000/health/ready    # also pings Qdrant

curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test","query":"when is my commission credited?"}'
```

A grounded answer returns `resolution_type: "RAG"` with a `sources` array and a
`retrieval_confidence` score. A question the knowledge base doesn't cover returns
`MANUAL_ESCALATION` with no sources — and makes no LLM call at all, by design.

A 502 mentioning `FIREWORKS_API_KEY` means `.env` isn't being picked up.

## Tests

The suite is fully offline — Fireworks is mocked with `respx`, Qdrant is monkeypatched — so it
needs no API key and no running services.

```bash
pytest -q
pytest tests/test_rag.py -q
pytest tests/test_rag.py::test_no_hits_escalates_without_calling_the_llm -q
```

Check that the backing services are reachable (separate from the app, so a failure points at
config rather than application code):

```bash
python scripts/check_services.py
```

## Layout

```
app/
  config.py                     # all settings, read from .env — nothing else touches os.environ
  main.py                       # FastAPI app, /health, /health/ready
  routers/chat.py               # POST /chat
  models/schemas.py             # request/response shapes
  services/
    rag.py                      # retrieval + grounded answer generation
    qdrant_store.py             # the only module that talks to Qdrant
    fireworks_client.py         # the only module that talks to Fireworks
    sparse.py                   # BM25 sparse vectors (fastembed, CPU)
    chunking.py                 # pure text chunking
data/
  categories.json               # the 10 categories — single source of truth
  kb/                           # knowledge-base articles (gitignored)
scripts/
  ingest.py                     # build the Qdrant collections
  check_services.py             # Qdrant/Redis/Postgres reachability
```

See [CLAUDE.md](CLAUDE.md) for the architectural invariants — particularly why retrieval confidence
comes from the dense similarity rather than the fused hybrid score.
