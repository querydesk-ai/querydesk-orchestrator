# QueryDesk — orchestrator task board

Derived from `SR Resolution Bot — Final PRD & 10-Day Roadmap.md`. This board tracks the
**orchestrator lane**. Ticketing lane items appear only where they gate a checkpoint.

Owner key: **O** = Orchestrator team · **T** = Ticketing team (service + deployment) · **B** = both
teams.

Status: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

## Where we are — 2026-10-17

- **Orchestrator lane: Days 1–8 done and merged (PRs #1–#14), plus the embedding trial.**
  Latest eval (94 rows, 2026-10-17): **routing accuracy 96%** (target ≥ 90%), **action requests
  answered with an article 0%** (target ≤ 5%), SR-bound category accuracy 100%, status rows 6/6.
  Final report: `docs/eval-report-v3.md` (orchestrator PR #17). It adds 10/10 notifications
  within a minute and an **answer-path p95 of 7.9 s** (target < 3 s, missed).
- **What's left, in order:**
  1. **Ticketing team:** review and merge contract v1.1 (querydesk-docs PR #1). Orchestrator PR #16
     already follows it.
  2. **Orchestrator team:** merge PR #17 (eval report v3 + README). The answer support check is done: 18 of
     20, two judges agreeing.
  3. **Ticketing team:** push the real ticketing service, then repeat Checkpoints 1–2 against it.
  4. **Day 9, both teams:** the 10 demo scenarios.
  5. **Day 10:** deploy (ticketing team), then the demo video and a smoke test of the live site
     (orchestrator team).
- **Day 5:**
  - Redis query-embedding cache (2.6 s → 30–60 ms on a repeat).
  - Local-embedding spike, then the full bge-base trial: not adopted
    ([embedding-trial.md](embedding-trial.md)).
  - Jev vs rules on the full eval set: **rules 97%, Jev 91%**, and Jev takes 3.5–7 s per call. Jev
    is kept as an **offline benchmark, not a fallback** ([jev-notes.md](jev-notes.md)). 22
    JevStation credits remain (they expire 2026-11-14), so don't spend them.
- **Checkpoint 1 met against a stand-in** (`tests/fakes/fake_ticketing.py`, PRD v1.1 contract).
  - The real check waits on the ticketing service, which is being built and not yet pushed.
  - `querydesk-docs` (contract v1.1) is written by the orchestrator team.
- **Infra:**
  - CI is green; the gitleaks licence secret is set.
  - Redis is reached over `REDIS_URL` (`rediss://` native protocol to Upstash, not its REST API).
  - Postgres (Neon) holds `sr_requests`, `notifications` and `decision_logs`
    (`scripts/migrate.py`; 001–003 applied, 003 on 2026-10-18).
- **Day 6 (slot filling) done**, merged in PRs #10 and #11. Verified live (stand-in ticketing,
  Fireworks, Upstash, Neon): "keep UAT-2 up for release testing" → asks for extend until → "till
  11 PM" → SR with approval, a row in `sr_requests`; the resend gets "already open"; out of scope →
  "flagged", with a `MANUAL_ESCALATION` row.
- **Day 7 (notifications) done**, merged in PR #12 ([plan-day-7.md](plan-day-7.md)).
  **Checkpoint 2 met against the stand-in:** approve in its Swagger → notification row in Neon in
  ~6 s; replayed webhook writes nothing; a webhook lost while the orchestrator was down is caught by
  `reconcile`. Migration 002 applied 2026-10-16.
- **Day 8 (status questions, notifications API, chat page) done**, merged in PR #13
  ([plan-day-8.md](plan-day-8.md)). Verified live in Chrome; eval
  (94 rows) 96% routing, 0% action answered, 6/6 status rows.
- **bge-base trial done, not adopted** ([embedding-trial.md](embedding-trial.md)): 30× faster
  embed (68 ms p50) but routing ≤ 91.5% at any thresholds and out-of-scope messages reach the SR
  path. Merged in PR #14: the `embed_backend` switch, set to `fireworks`. qwen3 thresholds unchanged
  (`confidence_mid` stays 0.49 by decision; `router_min_score` 0.48 rejected).
- **Known limitation for the Day 9 report:** first-time-message answer path p95 is above 3 s on
  qwen3 (embed ~2.4 s); mitigated by the Redis query cache. The BM25 model now loads at startup,
  so the first `/chat` after a boot no longer pays ~1.6 s for it.
- **Next up:** contract v1.1, then the Day 9 demo scenarios.
- **Repo housekeeping (checked 2026-10-17):**
  - "Automatically delete head branches" is still **off**. Ten merged branches remain on GitHub
    (e.g. `feat/day6-slot-filling`, `feat/embed-backend-trial`).
  - Two stacked PRs (#6, #8) merged into their base branch instead of `main`.
  - The repo is now **public**, so branch protection on `main` should now work (see Day 0).

---

## Day 0 — prerequisites (blocking)

These gate everything below. Nothing in Day 1 is verifiable until the first two are done.

- [x] **O** Create a Fireworks account and put a real key in `.env` as `FIREWORKS_API_KEY`.
  Verify: `embed_documents(["ping"])` returns a 4096-length vector (`qwen3-embedding-8b`;
  the PRD's nomic model is no longer served on Fireworks).
  *Blocks: all ingestion and the whole answer path.*
- [x] **O** Write the 11 knowledge-base articles into `data/kb/*.md` — one per category plus one
  general IT-policy doc. Each needs YAML front matter: `title`, `category_id`, `source_url`.
  *Blocks: ingestion has nothing to index. This is content work, not code.*
- [x] **B** Create the GitHub org (`querydesk-ai`) and the three repos (`querydesk-orchestrator`,
  `querydesk-ticketing`, `querydesk-docs`). *All three exist; ticketing and docs are still empty.*
- [ ] **B** Protect `main` on each repo. *Was blocked (403: private repos need a paid plan).
  `querydesk-orchestrator` is public as of 2026-10-17 and `main` is unprotected, so this can be
  done now. Until then, the PR-and-review rule is enforced by agreement only.*
- [~] **B** Approve API contract **v1.1** in `querydesk-docs` before either service builds on it.
  *Written 2026-10-17 as `openapi.yaml` (querydesk-docs PR #1); waiting on the ticketing team's review.
  Orchestrator PR #16 aligns with it: integer webhook `sr_id`, paginated `GET /sr`.*
  The four additions: `Idempotency-Key` on `POST /sr`; `updated_since` filter on `GET /sr`;
  `metadata.extend_until` → `revert_at` for `it_uat_extension`; signed
  `POST {orchestrator}/webhooks/sr-status` webhook (HMAC-SHA256, `X-Signature`, 5 retries).

---

## Day 1 — ingestion

**Done when:** contract v1.1 merged; both CI pipelines green.

- [x] **O** `git init`; `.gitignore` covering `.env`, `venv/`, `__pycache__/`, `.pytest_cache/`.
  *(`data/kb/` was ignored at first; the articles were written for this project and are versioned
  since 2026-10-15 so a clone can ingest and reproduce the eval.)* **Confirm `.env` is untracked
  before the first commit** — it holds live Qdrant,
  Postgres and Redis credentials.
- [x] **O** Pin dependencies (now in `pyproject.toml`; `requirements.txt` removed): add `fastembed` (sparse BM25, CPU) and `respx`
  (test mocking); pin `qdrant-client==1.19.1`; drop the duplicated `python-dotenv`; pin `redis`,
  `asyncpg`, `pytest`.
- [x] **O** Author `data/categories.json` — all 10 category ids with `name`, `domain`, `approval`
  (`none` / `always` / `conditional`), `required_fields`, `default_path`, and a prose `description`
  written for semantic matching. This file is the **single source of truth** shared with the
  ticketing service.
- [x] **O** Extend `core/config.py` with collection names, `embed_dim`, `retrieval_top_k`,
  `retrieval_prefetch_limit`, `chunk_chars`, `chunk_overlap`, `embed_batch_size`, and the
  `confidence_high` / `confidence_mid` placeholders (calibrated on Day 3, unused until then).
- [x] **O** `services/llm/fireworks.py`: add `embed_documents()` / `embed_query()` — documents as-is,
  queries wrapped in `QUERY_INSTRUCTION` (`qwen3-embedding-8b` replaced nomic and its prefixes), plus request
  batching at `embed_batch_size`. **Retrieval quality silently degrades without the query instruction.**
- [x] **O** `services/retrieval/chunking.py` — pure `chunk_text(text, size, overlap)`: paragraph-packing
  with overlap carry and a hard split for oversized paragraphs. No langchain.
- [x] **O** `services/retrieval/sparse.py` — wrap `fastembed.SparseTextEmbedding("Qdrant/bm25")`.
  Load the model once at module level; it is expensive to construct.
- [x] **O** `services/retrieval/qdrant_store.py` — `ensure_collections()` creating `kb_chunks` with named
  vectors `dense` (cosine, `embed_dim`) **and** `bm25` sparse with `modifier=Modifier.IDF`, plus a
  dense-only `categories` collection. Add batched `upsert_kb_chunks()` / `upsert_categories()`.
- [x] **O** `scripts/ingest.py --recreate` — parse front matter, chunk, dense + sparse embed, upsert
  both collections, print per-file chunk counts. Use a deterministic uuid5 point id over
  `source_path + chunk_index` so re-runs update in place instead of duplicating.
- [x] **O** Move `tests/test_check_services.py` → `scripts/check_services.py`. Its `test_`-prefixed
  filename is why `pytest` currently collects **zero** tests.
- [x] **O** `.github/workflows/ci.yml` — Python 3.11, `uv sync --locked`, `ruff check` + `ruff format --check`, `pytest -q`, **gitleaks**.
- [ ] **T** Spring Boot skeleton, Flyway migration for `service_requests`, `/health`, Swagger UI.

---

## Day 2 — hybrid retrieval and cited answers

**Done when:** 10 informational questions answered with sources; SR CRUD works in Swagger (P).

- [x] **O** `qdrant_store.hybrid_search(dense_vec, sparse_vec)` → `(fused_hits, top_dense_score)`.
  One `query_batch_points` call carrying two requests: a `FusionQuery(Fusion.RRF)` over dense +
  sparse `Prefetch` branches, and a dense-only `limit=1` query for the confidence score.
  **PRD Gap #2: fused RRF scores are rank-based — confidence must come from the dense similarity.**
- [x] **O** `services/rag.py::answer()` — embed query (dense + sparse), hybrid search, build a
  numbered context block, generate a grounded answer with `temperature=0.2`, return reply + sources
  + `top_dense_score`. No hits → `MANUAL_ESCALATION` with a fixed reply and **no LLM call**.
- [x] **O** `models/schemas.py` — `ResolutionType` enum (`RAG`, `MANUAL_ESCALATION` only for
  now; `AUTO_SR` lands with the decision layer), a `Source` model, and `sources` +
  `retrieval_confidence` on `ChatResponse`.
- [x] **O** `api/routes/chat.py` — delegate to `rag.answer()`; map `FireworksError` and
  `QdrantStoreError` to HTTP 502. Fix the `tags=[""]` typo.
- [x] **O** `main.py` — QueryDesk title, `ensure_collections()` in a lifespan handler so bad
  Qdrant config fails at boot, and `GET /health/ready` that pings Qdrant (keep `/health` cheap).
- [x] **O** Test suite, fully offline: `conftest.py` with an `anyio_backend` fixture;
  `test_chunking.py` (pure); `unit/test_fireworks.py` (**respx** — assert the query instruction is applied
  and batching splits correctly); `test_rag.py` (grounded vs escalation, asserting the escalation
  path does **not** call the LLM); `test_chat_endpoint.py` (`ASGITransport`, 200 + 502 mapping).
- [x] **O** Restructure into a `src/querydesk` package (`api/`, `core/`, `models/`, `services/llm`,
  `services/retrieval`) with `tests/unit` + `tests/integration`; one `UpstreamError` family → 502;
  `ensure_collections()` refuses a collection whose dense size ≠ `embed_dim`. KB re-indexed at 4096.
- [x] **O** **Acceptance gate:** run all 10 informational questions (2 per category) and confirm
  each returns a correct source citation. Separately verify the lexical half — query an exact error
  code or product name that appears verbatim in one article and confirm BM25 surfaces it. That case
  is the entire reason the PRD specifies hybrid rather than dense-only retrieval.
  *Done via the eval set: 20/20 informational questions cite the expected article at hit@1,
  including all 3 error-code (lexical) rows — see `docs/eval-report-v0.md`.*
- [ ] **T** `POST /sr`, `GET /sr/{id}`, `GET /sr` with filters; 400 on an unknown category.

---

## Day 3 — router + eval set

**Done when:** eval report v0 ✅ (T); every ticketing transition tested (P — open).

- [x] **O** Embed each message once: `services/retrieval/query.py::embed_query_vectors()`; the
  vectors are shared by retrieval and the router (`rag.answer(vectors=...)`), saving a Fireworks
  round trip per message.
- [x] **O** Semantic router over the `categories` collection; LLM tie-break only when the top two
  scores are close. *Built (`services/routing/router.py`); the tie-break is disabled after the eval —
  similarity alone 49/50, tie-break 44/50 at ~1.4 s (it answers `none` for informational
  questions because its prompt frames categories as requests).*
- [x] **O** Write the 60-query labelled eval set (30 action requests, 20 informational, 10
  out-of-scope/ambiguous). *In `eval/eval_set.jsonl` for now; moves to `querydesk-docs` once it
  exists.*
- [x] **O** Calibration script that sets `confidence_high` / `confidence_mid` from the eval set.
  *`scripts/eval_run.py` + `scripts/eval_report.py` → `docs/eval-report-v0.md`. Calibrated:
  high 0.56, mid 0.49, router_min 0.43. SR-bound category accuracy 100% (target ≥ 90%).*
- [x] **O** CI secrets scan: `gitleaks-action@v3` with a `GITLEAKS_LICENSE` secret. *The secret
  is set; the `secrets` job passes on every run (last checked on PR #14, 2026-10-17).*
- [ ] **T** Status state machine, `PATCH /sr/{id}/status`, 409 on invalid moves, status history;
  a unit test per transition.

### Day 3 follow-ups (carry forward)

- [x] **O** **Latency risk:** dense embedding is 1.6–2.2 s per query, so the answer path can't meet
  p95 < 3 s (generation adds ~1.2 s). *Decided: a Redis query cache shipped (Day 5); the local
  bge-base model was trialled and not adopted; the first BM25 load moved to startup. A first-time
  message still misses p95 < 3 s on qwen3, which is recorded as a known limitation for the final
  eval report ([embedding-trial.md](embedding-trial.md)).*
- [x] **O** Grow the eval set with near-miss rows before the Day 5 re-calibration —
  `router_min_score` 0.43 sits between an in-scope 0.439 and an out-of-scope 0.430, which is too
  tight to trust on 60 rows. *Done: 83 rows; `router_min_score` re-calibrated to 0.46 — every
  action request clears it.*
- [x] **B** Move `eval/eval_set.jsonl` to `querydesk-docs` once that repo has its first commit.
  *Done 2026-10-17: seeded on querydesk-docs `main` with the PRD and `categories.json`. The
  orchestrator keeps a synced copy; querydesk-docs is the source of truth.*

---

## Day 4 — decision layer · **Checkpoint 1**

**Done when:** the orchestrator creates a real SR in the ticketing service.
**Status:** all **O** items done and merged (PRs #3, #4), verified live against the stand-in
ticketing service (`tests/fakes/fake_ticketing.py`). The real checkpoint still waits on the
ticketing service's `POST /sr` and contract v1.1.

The failure that matters most is answering an action request with an article. Eval v0 showed that
**83% of action requests clear `confidence_high`**, so the decision layer must not answer on score
alone — it needs an explicit "is this an action?" signal (rule 2 says *not an action request* **and**
score ≥ high).

### Plumbing

- [x] **O** Split `services/rag.py` into `retrieve(query, vectors)` → hits + `top_dense_score` and
  `generate(query, hits)` → reply. The decision needs the score *before* deciding whether to
  generate; `answer()` stays as a thin wrapper so existing tests keep passing.
- [x] **O** Add `AUTO_SR` to `ResolutionType` — the decision layer can now emit it.

### Decision signals

- [x] **O** **Action detection** (`services/decision/signals.py`): is this message a request to *do*
  something, or a question? Start rule-based (imperative verbs, "please / pls / can you / need to",
  request verbs drawn from category descriptions), measured on the eval set. Target: action requests
  answered with an article ≤ 5%. Jev replaces this signal on Day 5 if it scores better.
- [x] **O** **Status-question detection** for rule 1 ("status of my requests", "what happened to my
  SR"). Only the routing here; the actual `STATUS_LOOKUP` answer is Day 8.
- [x] **O** **Required-field extraction** (`services/decision/extraction.py`): one LLM call returning
  JSON for the category's `required_fields` read from `categories.json` (invariant #3 — never
  hardcoded), plus the list still missing. Decides RAISE_SR vs COLLECT_DETAILS. Must not invent
  values: a field absent from the message is missing, never guessed.

### Rule engine

- [x] **O** `DecisionEngine` interface (`services/decision/engine.py`): `async decide(DecisionInput)
  -> DecisionOutput` with `decision`, `resolution_type`, `category_id`, `fields`, `missing_fields`,
  `requires_approval`, `engine`. Selected by a `decision_engine` setting (`rules` now, `jev` Day 5).
- [x] **O** `RuleEngine` — the PRD's six rules, first match wins:
  1. asks about an existing request → `STATUS_LOOKUP`
  2. not an action, score ≥ `confidence_high` → `ANSWER` (`RAG`)
  3. not an action, score ≥ `confidence_mid` → `ANSWER_WITH_DISCLAIMER` + offer to raise an SR
  4. action **and** router confident → `RAISE_SR` (`AUTO_SR`) if fields complete,
     else `COLLECT_DETAILS` *(narrower than the PRD's "or score < mid" — a question never opens
     an SR; see plan-days-4-5.md)*
  5. router not confident / outside the 10 categories / unanswered question → `ESCALATE`
     (`MANUAL_ESCALATION`)
  6. config rules apply last and can only **tighten**: force approval (e.g. prod restarts for a
     `conditional` category) or force escalation of a category's SRs.
- [x] **O** Unit tests: one per rule, the first-match ordering, and that rule 6 can tighten but never
  loosen. All offline.

### Ticketing client

- [x] **O** `services/ticketing/client.py` — the only module that talks to the ticketing service:
  `create_sr()` (`POST /sr` with an `Idempotency-Key` header) and `get_sr()`. Raises
  `TicketingError(UpstreamError)`, so `/chat` maps it to 502 with no router change.
  `ticketing_base_url` in config.
- [x] **O** Idempotency key derived deterministically from the request (user, category, normalised
  fields), so a retried or double-sent message returns the original SR instead of a duplicate.
- [x] **O** `respx` tests: request shape, idempotency header, 200-on-repeat handling, error mapping.
  Field names follow the PRD until the OpenAPI file lands in `querydesk-docs` — **re-check then.**

### Wiring and measurement

- [x] **O** Wire into `POST /chat`: embed once → retrieve + route (concurrently) → decide → generate
  (ANSWER paths) / create SR (RAISE_SR) / ask for the missing fields (COLLECT_DETAILS, single turn —
  Redis state is Day 6) / escalate. Add `decision` (and `sr_id` when one is created) to
  `ChatResponse`.
- [x] **O** Log every decision (F10) as one structured log line with inputs, outputs, engine and
  `latency_ms`. *Since 2026-10-18 the same record is also written to `decision_logs`
  (`003_decision_logs.sql`, applied to Neon), in the background so it adds no latency.*
- [x] **O** Extend `eval_run.py` / `eval_report.py` with **routing accuracy** (decision matches the
  label, target ≥ 90%) and **action requests answered with an article** (target ≤ 5%). This becomes
  the baseline the Day 5 Jev engine has to beat. *Result: 96% and 0% — `docs/eval-report-v1.md`.*
- [x] **O** Decision layer latency under 500 ms on the eval run, excluding the LLM extraction call
  (report it separately). *Result: ~0 ms; extraction p50 2.0 s / p95 3.6 s on the SR path.*

### Jev and the checkpoint

- [x] **O** *(moved to Day 5)* Jev Playground session: confirm the real request/response shape for the three questions
  (action? existing request? does the top article answer it — probability) and write it down in
  `docs/`. *Done on Day 5 through JevStation; recorded in [jev-notes.md](jev-notes.md).*
- [ ] **T** Idempotency key on `POST /sr`, Testcontainers integration tests, Dockerfile.
- [~] **B** **Checkpoint 1:** a `/chat` message ("restart the claims app in UAT, it's hung") creates
  a real SR in the ticketing service, and resending it returns the same SR. *Verified live against
  the stand-in (SR 1001 created, resend returned 1001); repeat against the real service.*

---

## Days 5–10

Days 5–8 were each planned in their own doc (linked per day). Days 9–10 are headline items until
their planning pass.

### Day 5 — Jev, and the latency decision
*Done when: routing accuracy reported for both engines; a written recommendation on embedding
latency.* Detail in [plan-days-4-5.md](plan-days-4-5.md#day-5--jev-and-the-latency-decision).
- [x] **O** Jev access confirmed and recorded in [jev-notes.md](jev-notes.md). *Through JevStation
  (per-evaluation credits, 12 questions per request); 3.5–7 s per live call vs a 500 ms budget.*
- [x] **O** `services/llm/jev.py` — the only module that talks to Jev; `JevError(UpstreamError)`.
- [x] **O** Both engines on the full eval set (`scripts/jev_compare.py`, batches of 10, cached,
  27 credits): **rules 97% vs Jev 91%** routing accuracy; action answered 0% vs 3%. **Rules stays
  the default.**
- [x] **O** Decision: Jev is kept as an **offline benchmark, not a fallback** — slower, less
  reliable and less accurate than the rule engine
  ([jev-notes.md](jev-notes.md#decision-2026-10-15)).
- [ ] ~~**O** Live `JevEngine` with a rules fallback~~ — *not built: every live call would exceed the
  decision budget and spend a credit. Revisit only with a direct TypeSafe key.*
- [x] **O** Latency spike: a local fastembed dense model vs `qwen3-embedding-8b` on the eval set
  (speed, hit@1, router accuracy); written recommendation before Day 8. *Done 2026-10-15: Redis
  query cache shipped (2.6 s → 30–60 ms on a repeat); spike in `scripts/latency_spike.py`. Keep
  qwen3 for now and trial `bge-base-en-v1.5` (97% action routing, 36 ms) through the full eval —
  [latency-recommendation.md](latency-recommendation.md).*
- [x] **O** bge-base trial behind an `embed_backend` setting, separate collections, full eval +
  re-calibration; adopt only if it holds routing ≥ 95% and no wrong-category SRs. Before Day 8.
  *Done 2026-10-17: not adopted — best 91.5% routing, 11/18 out-of-scope escalated (qwen3 96%,
  15/18). Switch kept, off. [embedding-trial.md](embedding-trial.md).*
- [ ] **T** Webhook outbox with HMAC signing and backoff retry.

### Day 6 — slot filling
*Done when: a UAT extension SR raised from one message plus one follow-up.* Detail in
[plan-day-6.md](plan-day-6.md).
- [x] **O** `services/conversation.py`: `conversation:{user_id}` in Redis (30-min TTL) via new
  `redis_store` JSON helpers. Redis down means single-turn, never a 500.
- [x] **O** Slot-filling flow in `pipeline.handle_message`: cancel; re-extract on the original
  message plus follow-ups and merge; a new ask resets; complete raises the SR; at most 2
  follow-ups, then ESCALATE. *A cancel is the new `CANCELLED` decision.*
- [x] **O** Verification fixes (live check of PR #10 failed on a vague start):
  - follow-ups are extracted from the reply only (`extract_follow_up`), never from a joined
    transcript, which had copied the opening sentence into `reason`;
  - every follow-up value must appear in the reply (`_grounded`). Not applied to the first
    message: grounding there dropped `change_type: "firewall change"` on eval act-13;
  - a stated value may correct a collected one ("it's UAT-3");
  - a reply with no field that makes no sense on its own counts as an unhelpful follow-up instead
    of silently dropping the conversation; the cap escalation records the conversation and names
    what's missing;
  - a bare "cancel" with nothing open records nothing; a duplicate is reported as `RAISE_SR`.
  - Eval re-run (88 rows, 2026-10-15, thresholds unchanged): routing accuracy 95% (strict 93%),
    action answered 0%, SR-bound category accuracy 100%, hit@k 100%. The only miss beyond v2's
    three is the new near-miss row `oos-18` ("stop the claims app in UAT" → UAT extension, 0.4969
    vs restart 0.4925): out of scope, but routed. Left for the router work, not tuned by hand.
- [x] **O** `RuleEngine.complete_sr()`: rule 6 applies to the merged fields (e.g. a prod
  environment given in the follow-up still needs approval).
- [x] **O** `services/state/postgres_store.py` + `migrations/001_sr_requests.sql` +
  `scripts/migrate.py`. Record raised SRs and escalations; a failed write never loses the SR.
- [x] **O** Record every `ESCALATE`, then change `ESCALATE_REPLY` to "flagged for the service desk"
  (falling back to "contact the desk" if the write fails).
- [x] **O** Duplicate check (F5): `ticketing.list_open_srs(user_id)` before creating; an open SR
  in the same category gets an "already open" reply. *Also checked before asking for details, so
  nobody fills in a form for an SR they can't have (found in the live run).*
- [ ] **T** UAT auto-revert scheduler; approver and note on status changes.

### Day 7 — notifications · **Checkpoint 2**
*Done when: approve in Swagger, user notified within 1 minute.*
Detail in [plan-day-7.md](plan-day-7.md).
- [x] **O** `POST /webhooks/sr-status`: HMAC-SHA256 hex `X-Signature` over the raw body
  (`services/notifications/signing.py`); 401 / 422 / 503 (broker down → the outbox retries); 202
  once queued. No DB work on the request.
- [x] **O** Celery app (`querydesk.worker`, Celery 5.6.3): `notify_status_change` (retried on
  `UpstreamError`) and a beat `reconcile` every 10 min via `GET /sr?updated_since=` with a Redis
  watermark. One event loop per worker process; `--pool=solo` on Windows.
- [x] **O** `migrations/002_notifications.sql`: `UNIQUE (sr_id, status)` makes retries, replays and
  reconcile overlaps no-ops; `sr_requests` only moves forward (`STATUS_RANK`).
- [x] **O** Stand-in ticketing sends signed webhooks (`STAND_IN_WEBHOOK_URL`) and filters on
  `updated_since`.
- [x] **B** **Checkpoint 2** against the stand-in: ~6 s from approval to notification. *Repeat
  against the real service.*
- [ ] **O** Measure an idle worker's Upstash command use on the dashboard (INFO counters are
  unreliable on Upstash).
- [x] **O** Write the webhook signature format into contract v1.1 (`querydesk-docs`). *In
  `openapi.yaml`: lowercase hex HMAC-SHA256 of the raw body, no prefix, with a worked example.*
- [ ] **T** Postman collection that plays the approver; seed data.

### Day 8 — chat page
*Done when: the full stack starts with one command.*
Detail in [plan-day-8.md](plan-day-8.md).
- [x] **O** Status questions (`services/status.py`): open SRs, or one SR by number; someone else's
  SR reads like a missing one; status turns skip embedding and search.
- [x] **O** `GET /notifications`, `POST /notifications/read` (scoped by `user_id`).
- [x] **O** Minimal chat page at `/` (one static file, polls notifications every 10 s).
- [x] **O** Eval: `status` kind + 6 rows; 96% routing, thresholds unchanged.
- [x] **O** Chat page at phone width (390 px iframe): no horizontal scroll. Light theme not yet
  seen.
- [x] **O** Orchestrator image and `compose.yaml` (api, worker, beat, local Redis, stand-in
  profile); CI builds both targets. Run locally on 2026-10-26: answer, SR, webhook and
  notification all work in compose. Deployment notes for the ticketing team are in
  [docker.md](docker.md).
- [~] **O** WhatsApp chat through Meta's Cloud API or Twilio (`/webhooks/whatsapp`, migration 004): a
  private one-to-one chat with the same pipeline, plus SR updates in the same thread. *Offline
  tests pass; the live phone test needs a tunnel ([whatsapp.md](whatsapp.md)).*
- [ ] **T** Docker Compose for the full stack, Nginx, structured logs with a correlation id.

### Day 9 — demo scenarios · **Checkpoint 3**
*Done when: all 10 scenarios pass.*
- [ ] **B** Run the 10 demo scenarios together (one end-to-end run per category); fix bugs.
- [~] **O** Final eval report (the PRD's "eval report v1"; v0–v2 already exist). *Drafted as
  `docs/eval-report-v3.md` (PR #17): every target measured except answer support, which waits on
  the hand judgement in `docs/answer-support-v1.md`. The latency miss is recorded (p95 7.9 s end
  to end).* **T** Contract tests against the OpenAPI file.

### Day 10 — ship
*Done when: v1.0 tagged and live.*
- [~] **O** READMEs crediting both teams, architecture diagram, demo video; smoke-test the live site.
  *Orchestrator README done (PR #17; ownership is credited by team). The demo video and smoke
  test need the deployed site.*
- [ ] **T** EC2 t3.medium setup, Compose deploy, secrets, smoke test.

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
- `main` is protected; the other team reviews every PR, so both teams can explain both services.
- A 15-minute sync at a fixed time daily; one GitHub Projects board.
- Checkpoint days don't move — if a day runs over, cut scope before the checkpoint.
- Plan assumes 3–4 focused hours a day each; if short, stretch to 14 days and keep the checkpoints.
