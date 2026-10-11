# QueryDesk — orchestrator task board

Derived from `SR Resolution Bot — Final PRD & 10-Day Roadmap.md`. This board tracks the
**orchestrator lane (Tanmay)**. Partner lane items appear only where they gate a checkpoint.

Owner key: **T** = Tanmay (orchestrator) · **P** = Partner (ticketing + deploy) · **B** = both.

Status: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

## Where we are — 2026-10-11

- **Orchestrator lane: Days 1–3 done** and merged to `main` (PR #1). CI green (tests + gitleaks).
  Eval v0: SR-bound category accuracy 100%, informational citations 20/20.
- **Partner lane: not started.** `querydesk-ticketing` and `querydesk-docs` are empty, so contract
  v1.1 is unapproved and every **P** item from Day 1 on is open. **This blocks Checkpoint 1 (Day 4).**
- **Next up: Day 4 orchestrator work**, built against the PRD's v1.1 contract with mocked
  ticketing calls, so it is ready the moment the ticketing service exists.

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
- [x] **B** Create the GitHub org (`querydesk-ai`) and the three repos (`querydesk-orchestrator`,
  `querydesk-ticketing`, `querydesk-docs`). *All three exist; ticketing and docs are still empty.*
- [!] **B** Protect `main` on each repo. *Blocked: GitHub returns 403 — branch protection on
  private repos needs a paid plan (Team/Pro) or public repos. Until then, the PR-and-review rule is
  enforced by agreement only.*
- [ ] **B** Approve API contract **v1.1** in `querydesk-docs` before either service builds on it.
  **Now the critical path** — the Day 4 ticketing client and Checkpoint 1 depend on it.
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
- [x] **T** **Acceptance gate:** run all 10 informational questions (2 per category) and confirm
  each returns a correct source citation. Separately verify the lexical half — query an exact error
  code or product name that appears verbatim in one article and confirm BM25 surfaces it. That case
  is the entire reason the PRD specifies hybrid rather than dense-only retrieval.
  *Done via the eval set: 20/20 informational questions cite the expected article at hit@1,
  including all 3 error-code (lexical) rows — see `docs/eval-report-v0.md`.*
- [ ] **P** `POST /sr`, `GET /sr/{id}`, `GET /sr` with filters; 400 on an unknown category.

---

## Day 3 — router + eval set

**Done when:** eval report v0 ✅ (T); every ticketing transition tested (P — open).

- [x] **T** Embed each message once: `services/retrieval/query.py::embed_query_vectors()`; the
  vectors are shared by retrieval and the router (`rag.answer(vectors=...)`), saving a Fireworks
  round trip per message.
- [x] **T** Semantic router over the `categories` collection; LLM tie-break only when the top two
  scores are close. *Built (`services/routing/router.py`); the tie-break is disabled after the eval —
  similarity alone 49/50, tie-break 44/50 at ~1.4 s (it answers `none` for informational
  questions because its prompt frames categories as requests).*
- [x] **T** Write the 60-query labelled eval set (30 action requests, 20 informational, 10
  out-of-scope/ambiguous). *In `eval/eval_set.jsonl` for now; moves to `querydesk-docs` once it
  exists.*
- [x] **T** Calibration script that sets `confidence_high` / `confidence_mid` from the eval set.
  *`scripts/eval_run.py` + `scripts/eval_report.py` → `docs/eval-report-v0.md`. Calibrated:
  high 0.56, mid 0.49, router_min 0.43. SR-bound category accuracy 100% (target ≥ 90%).*
- [x] **T** CI fixed: `gitleaks-action` needs a paid licence for org repos, so the secrets job had
  failed on every run; it now runs the MIT gitleaks CLI. Merged in PR #1.
- [ ] **P** Status state machine, `PATCH /sr/{id}/status`, 409 on invalid moves, status history;
  a unit test per transition.

### Day 3 follow-ups (carry forward)

- [!] **T** **Latency risk:** dense embedding is 1.6–2.2 s per query, so the answer path can't meet
  p95 < 3 s (generation adds ~1.2 s). Decide before Day 8: a local CPU embedding model via fastembed
  (needs `--recreate` and a re-calibration), caching repeated queries, or a faster hosted model.
- [ ] **T** Grow the eval set with near-miss rows before the Day 5 re-calibration —
  `router_min_score` 0.43 sits between an in-scope 0.439 and an out-of-scope 0.430, which is too
  tight to trust on 60 rows.
- [ ] **B** Move `eval/eval_set.jsonl` to `querydesk-docs` once that repo has its first commit.

---

## Day 4 — decision layer · **Checkpoint 1**

**Done when:** the orchestrator creates a real SR in the ticketing service.
**Blocked on:** contract v1.1 and the partner's `POST /sr` (both repos are still empty). Everything
marked **T** below can be built and tested now against the PRD contract with `respx` mocks.

The failure that matters most is answering an action request with an article. Eval v0 showed that
**83% of action requests clear `confidence_high`**, so the decision layer must not answer on score
alone — it needs an explicit "is this an action?" signal (rule 2 says *not an action request* **and**
score ≥ high).

### Plumbing

- [ ] **T** Split `services/rag.py` into `retrieve(query, vectors)` → hits + `top_dense_score` and
  `generate(query, hits)` → reply. The decision needs the score *before* deciding whether to
  generate; `answer()` stays as a thin wrapper so existing tests keep passing.
- [ ] **T** Add `AUTO_SR` to `ResolutionType` — the decision layer can now emit it.

### Decision signals

- [ ] **T** **Action detection** (`services/decision/signals.py`): is this message a request to *do*
  something, or a question? Start rule-based (imperative verbs, "please / pls / can you / need to",
  request verbs drawn from category descriptions), measured on the eval set. Target: action requests
  answered with an article ≤ 5%. Jev replaces this signal on Day 5 if it scores better.
- [ ] **T** **Status-question detection** for rule 1 ("status of my requests", "what happened to my
  SR"). Only the routing here; the actual `STATUS_LOOKUP` answer is Day 8.
- [ ] **T** **Required-field extraction** (`services/decision/extraction.py`): one LLM call returning
  JSON for the category's `required_fields` read from `categories.json` (invariant #3 — never
  hardcoded), plus the list still missing. Decides RAISE_SR vs COLLECT_DETAILS. Must not invent
  values: a field absent from the message is missing, never guessed.

### Rule engine

- [ ] **T** `DecisionEngine` interface (`services/decision/engine.py`): `async decide(DecisionInput)
  -> DecisionOutput` with `decision`, `resolution_type`, `category_id`, `fields`, `missing_fields`,
  `requires_approval`, `engine`. Selected by a `decision_engine` setting (`rules` now, `jev` Day 5).
- [ ] **T** `RuleEngine` — the PRD's six rules, first match wins:
  1. asks about an existing request → `STATUS_LOOKUP`
  2. not an action, score ≥ `confidence_high` → `ANSWER` (`RAG`)
  3. not an action, score ≥ `confidence_mid` → `ANSWER_WITH_DISCLAIMER` + offer to raise an SR
  4. action (or score < mid) **and** router confident → `RAISE_SR` (`AUTO_SR`) if fields complete,
     else `COLLECT_DETAILS`
  5. router not confident / outside the 10 categories → `ESCALATE` (`MANUAL_ESCALATION`)
  6. config rules apply last and can only **tighten**: force approval (e.g. prod restarts for a
     `conditional` category) or force escalation for a category.
- [ ] **T** Unit tests: one per rule, the first-match ordering, and that rule 6 can tighten but never
  loosen. All offline.

### Ticketing client

- [ ] **T** `services/ticketing/client.py` — the only module that talks to the ticketing service:
  `create_sr()` (`POST /sr` with an `Idempotency-Key` header) and `get_sr()`. Raises
  `TicketingError(UpstreamError)`, so `/chat` maps it to 502 with no router change.
  `ticketing_base_url` in config.
- [ ] **T** Idempotency key derived deterministically from the request (user, category, normalised
  fields), so a retried or double-sent message returns the original SR instead of a duplicate.
- [ ] **T** `respx` tests: request shape, idempotency header, 200-on-repeat handling, error mapping.
  Field names follow the PRD until the OpenAPI file lands in `querydesk-docs` — **re-check then.**

### Wiring and measurement

- [ ] **T** Wire into `POST /chat`: embed once → retrieve + route (concurrently) → decide → generate
  (ANSWER paths) / create SR (RAISE_SR) / ask for the missing fields (COLLECT_DETAILS, single turn —
  Redis state is Day 6) / escalate. Add `decision` (and `sr_id` when one is created) to
  `ChatResponse`.
- [ ] **T** Log every decision (F10) as one structured log line with inputs, outputs, engine and
  `latency_ms`; it moves into the `decision_logs` table with Postgres on Day 6.
- [ ] **T** Extend `eval_run.py` / `eval_report.py` with **routing accuracy** (decision matches the
  label, target ≥ 90%) and **action requests answered with an article** (target ≤ 5%). This becomes
  the baseline the Day 5 Jev engine has to beat.
- [ ] **T** Decision layer latency under 500 ms on the eval run, excluding the LLM extraction call
  (report it separately).

### Jev and the checkpoint

- [ ] **T** Jev Playground session: confirm the real request/response shape for the three questions
  (action? existing request? does the top article answer it — probability) and write it down in
  `docs/`. `TYPESAFE_API_KEY` is first used here.
- [ ] **P** Idempotency key on `POST /sr`, Testcontainers integration tests, Dockerfile.
- [ ] **B** **Checkpoint 1:** a `/chat` message ("restart the claims app in UAT, it's hung") creates
  a real SR in the ticketing service, and resending it returns the same SR.

---

## Days 5–10 — not yet planned in detail

Headline items only, so the board is complete. Each gets its own planning pass when its turn comes.

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
