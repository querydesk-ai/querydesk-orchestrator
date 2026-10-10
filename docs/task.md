# QueryDesk — orchestrator task board

Derived from `SR Resolution Bot — Final PRD & 10-Day Roadmap.md`. This board tracks the
**orchestrator lane (Tanmay)**. Partner lane items appear only where they gate a checkpoint.

Owner key: **T** = Tanmay (orchestrator) · **P** = Partner (ticketing + deploy) · **B** = both.

Status: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Day 0 — prerequisites (blocking)

These gate everything below. Nothing in Day 1 is verifiable until the first two are done.

- [x] **T** Create a Fireworks account and put a real key in `.env` as `FIREWORKS_API_KEY`.
  Verify: `embed_documents(["ping"])` returns a 4096-length vector (`qwen3-embedding-8b`;
  the PRD's nomic model is no longer served on Fireworks).
  *Blocks: all ingestion and the whole answer path.*
- [x] **T** Write the 11 knowledge-base articles into `data/kb/*.md` — one per category plus one
  general IT-policy doc. Each needs YAML front matter: `title`, `category_id`, `source_url`.
  *Blocks: ingestion has nothing to index. This is content work, not code.*
- [ ] **B** Create the GitHub org and the three repos (`querydesk-orchestrator`,
  `querydesk-ticketing`, `querydesk-docs`); protect `main` on each.
- [ ] **B** Approve API contract **v1.1** in `querydesk-docs` before either service builds on it.
  The four additions: `Idempotency-Key` on `POST /sr`; `updated_since` filter on `GET /sr`;
  `metadata.extend_until` → `revert_at` for `it_uat_extension`; signed
  `POST {orchestrator}/webhooks/sr-status` webhook (HMAC-SHA256, `X-Signature`, 5 retries).

---

## Day 1 — ingestion

**Done when:** contract v1.1 merged; both CI pipelines green.

- [x] **T** `git init`; `.gitignore` covering `.env`, `venv/`, `__pycache__/`, `.pytest_cache/`,
  `data/kb/`. **Confirm `.env` is untracked before the first commit** — it holds live Qdrant,
  Postgres and Redis credentials.
- [x] **T** Pin dependencies (now in `pyproject.toml`; `requirements.txt` removed): add `fastembed` (sparse BM25, CPU) and `respx`
  (test mocking); pin `qdrant-client==1.19.1`; drop the duplicated `python-dotenv`; pin `redis`,
  `asyncpg`, `pytest`.
- [x] **T** Author `data/categories.json` — all 10 category ids with `name`, `domain`, `approval`
  (`none` / `always` / `conditional`), `required_fields`, `default_path`, and a prose `description`
  written for semantic matching. This file is the **single source of truth** shared with the
  ticketing service.
- [x] **T** Extend `core/config.py` with collection names, `embed_dim`, `retrieval_top_k`,
  `retrieval_prefetch_limit`, `chunk_chars`, `chunk_overlap`, `embed_batch_size`, and the
  `confidence_high` / `confidence_mid` placeholders (calibrated on Day 3, unused until then).
- [x] **T** `services/llm/fireworks.py`: add `embed_documents()` / `embed_query()` — documents as-is,
  queries wrapped in `QUERY_INSTRUCTION` (`qwen3-embedding-8b` replaced nomic and its prefixes), plus request
  batching at `embed_batch_size`. **Retrieval quality silently degrades without the query instruction.**
- [x] **T** `services/retrieval/chunking.py` — pure `chunk_text(text, size, overlap)`: paragraph-packing
  with overlap carry and a hard split for oversized paragraphs. No langchain.
- [x] **T** `services/retrieval/sparse.py` — wrap `fastembed.SparseTextEmbedding("Qdrant/bm25")`.
  Load the model once at module level; it is expensive to construct.
- [x] **T** `services/retrieval/qdrant_store.py` — `ensure_collections()` creating `kb_chunks` with named
  vectors `dense` (cosine, `embed_dim`) **and** `bm25` sparse with `modifier=Modifier.IDF`, plus a
  dense-only `categories` collection. Add batched `upsert_kb_chunks()` / `upsert_categories()`.
- [x] **T** `scripts/ingest.py --recreate` — parse front matter, chunk, dense + sparse embed, upsert
  both collections, print per-file chunk counts. Use a deterministic uuid5 point id over
  `source_path + chunk_index` so re-runs update in place instead of duplicating.
- [x] **T** Move `tests/test_check_services.py` → `scripts/check_services.py`. Its `test_`-prefixed
  filename is why `pytest` currently collects **zero** tests.
- [x] **T** `.github/workflows/ci.yml` — Python 3.11, `uv sync --locked`, `ruff check` + `ruff format --check`, `pytest -q`, **gitleaks**.
- [ ] **P** Spring Boot skeleton, Flyway migration for `service_requests`, `/health`, Swagger UI.

---

## Day 2 — hybrid retrieval and cited answers

**Done when:** 10 informational questions answered with sources; SR CRUD works in Swagger (P).

- [x] **T** `qdrant_store.hybrid_search(dense_vec, sparse_vec)` → `(fused_hits, top_dense_score)`.
  One `query_batch_points` call carrying two requests: a `FusionQuery(Fusion.RRF)` over dense +
  sparse `Prefetch` branches, and a dense-only `limit=1` query for the confidence score.
  **PRD Gap #2: fused RRF scores are rank-based — confidence must come from the dense similarity.**
- [x] **T** `services/rag.py::answer()` — embed query (dense + sparse), hybrid search, build a
  numbered context block, generate a grounded answer with `temperature=0.2`, return reply + sources
  + `top_dense_score`. No hits → `MANUAL_ESCALATION` with a fixed reply and **no LLM call**.
- [x] **T** `models/schemas.py` — `ResolutionType` enum (`RAG`, `MANUAL_ESCALATION` only for
  now; `AUTO_SR` lands with the decision layer), a `Source` model, and `sources` +
  `retrieval_confidence` on `ChatResponse`.
- [x] **T** `api/routes/chat.py` — delegate to `rag.answer()`; map `FireworksError` and
  `QdrantStoreError` to HTTP 502. Fix the `tags=[""]` typo.
- [x] **T** `main.py` — QueryDesk title, `ensure_collections()` in a lifespan handler so bad
  Qdrant config fails at boot, and `GET /health/ready` that pings Qdrant (keep `/health` cheap).
- [x] **T** Test suite, fully offline: `conftest.py` with an `anyio_backend` fixture;
  `test_chunking.py` (pure); `unit/test_fireworks.py` (**respx** — assert the query instruction is applied
  and batching splits correctly); `test_rag.py` (grounded vs escalation, asserting the escalation
  path does **not** call the LLM); `test_chat_endpoint.py` (`ASGITransport`, 200 + 502 mapping).
- [x] **T** Restructure into a `src/querydesk` package (`api/`, `core/`, `models/`, `services/llm`,
  `services/retrieval`) with `tests/unit` + `tests/integration`; one `UpstreamError` family → 502;
  `ensure_collections()` refuses a collection whose dense size ≠ `embed_dim`. KB re-indexed at 4096.
- [~] **T** **Acceptance gate:** run all 10 informational questions (2 per category) and confirm
  each returns a correct source citation. Separately verify the lexical half — query an exact error
  code or product name that appears verbatim in one article and confirm BM25 surfaces it. That case
  is the entire reason the PRD specifies hybrid rather than dense-only retrieval.
  *Smoke-tested 3 questions (password reset, commission, UAT extension) — correct citations.
  Full 10 + the lexical check still to run.*
- [ ] **P** `POST /sr`, `GET /sr/{id}`, `GET /sr` with filters; 400 on an unknown category.

---

## Days 3–10 — not yet planned in detail

Headline items only, so the board is complete. Each needs its own planning pass; Days 4–5 in
particular depend on facts the PRD says are unknown until the Day 4 Jev Playground session.

### Day 3 — router + eval set
*Done when: eval report v0; every ticketing transition tested.*
- [ ] **T** Semantic router over the `categories` collection; LLM tie-break only when the top two
  scores are close.
- [ ] **T** Write the 60-query labelled eval set (30 action requests, 20 informational, 10
  out-of-scope/ambiguous) in `querydesk-docs`.
- [ ] **T** Calibration script that sets `confidence_high` / `confidence_mid` from the eval set.
- [ ] **P** Status state machine, `PATCH /sr/{id}/status`, 409 on invalid moves, status history.

### Day 4 — decision layer · **Checkpoint 1**
*Done when: the orchestrator creates a real SR in the ticketing service.*
- [ ] **T** Decision layer interface + **rule engine first** (6 rules, first match wins).
- [ ] **T** Ticketing service client with idempotency key.
- [ ] **T** Try Jev in the Playground and confirm its real request shape.
- [ ] **P** Idempotency key, Testcontainers integration tests, Dockerfile.

### Day 5 — Jev
*Done when: routing accuracy reported for both engines.*
- [ ] **T** Jev implementation behind the existing interface; run both engines on the eval set; the
  higher-accuracy one becomes the default.
- [ ] **P** Webhook outbox with HMAC signing and backoff retry.

### Day 6 — slot filling
*Done when: a UAT extension SR raised from one message plus one follow-up.*
- [ ] **T** Redis slot-filling state (`conversation:{user_id}`, 30-min TTL, max 2 follow-up turns);
  duplicate check (no second open SR per user + category); `sr_requests` table.
- [ ] **P** UAT auto-revert scheduler; approver and note on status changes.

### Day 7 — notifications · **Checkpoint 2**
*Done when: approve in Swagger, user notified within 1 minute.*
- [ ] **T** Webhook receiver with signature verification; Celery notify task; 10-minute
  reconciliation poll via `GET /sr?updated_since=`. Use the quiet worker flags in dev.
- [ ] **P** Postman collection that plays the approver; seed data.

### Day 8 — chat page
*Done when: the full stack starts with one command.*
- [ ] **T** Status questions, notifications endpoint, minimal chat page.
- [ ] **P** Docker Compose for the full stack, Nginx, structured logs with a correlation id.

### Day 9 — demo scenarios · **Checkpoint 3**
*Done when: all 10 scenarios pass.*
- [ ] **B** Run the 10 demo scenarios together (one end-to-end run per category); fix bugs.
- [ ] **T** Eval report v1. **P** Contract tests against the OpenAPI file.

### Day 10 — ship
*Done when: v1.0 tagged and live.*
- [ ] **T** READMEs crediting both people, architecture diagram, demo video; smoke-test the live site.
- [ ] **P** EC2 t3.medium setup, Compose deploy, secrets, smoke test.

---

## Success metrics (PRD targets)

| Metric | v1 target |
| --- | --- |
| Routing accuracy | ≥ 90% |
| Category accuracy on SR-bound queries | ≥ 90% |
| Action requests wrongly answered with an article | ≤ 5% |
| Answers supported by the cited article | 18 of 20 |
| p95 latency, answer path | < 3 s (decision layer < 500 ms) |
| Status changes notified within 1 minute | 10 of 10 |

The failure that matters most is **answering an action request with an article** — "restart the
claims app in UAT" must raise an SR, not explain the restart process.

## Working agreements

- Contract first: no endpoint or field change without an approved PR in `querydesk-docs`.
- `main` is protected; the other person reviews every PR, so each of you can explain both services.
- A 15-minute sync at a fixed time daily; one GitHub Projects board.
- Checkpoint days don't move — if a day runs over, cut scope before the checkpoint.
- Plan assumes 3–4 focused hours a day each; if short, stretch to 14 days and keep the checkpoints.
