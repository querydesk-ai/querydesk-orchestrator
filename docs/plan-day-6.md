# Day 6 plan: slot filling, duplicate check, `sr_requests`

**Done when:** a UAT extension SR is raised from one message plus one follow-up (PRD user story 3:
"a user who leaves out details is asked only for the missing ones, then gets the SR number").

PRD requirements covered:
- **F4:** collect the required fields in at most 2 follow-up turns.
- **F5:** no second open SR per user and category.
- **The orchestrator's `sr_requests` table.**

## Where things stand

- `COLLECT_DETAILS` is single-turn. `services/pipeline.py::_act` asks for the missing fields and
  forgets them, so the user's answer starts from scratch.
- Escalations aren't recorded anywhere, which is why `ESCALATE_REPLY` tells people to contact the
  desk themselves.
- **Redis:** reached over `REDIS_URL` (`rediss://…upstash.io:6379`), the native Redis protocol with
  TLS, through `redis.asyncio` in `services/state/redis_store.py`. Not Upstash's REST API. The Day 7
  Celery broker needs the native protocol too. Budget: about 2–4 Upstash commands per message
  (query cache plus conversation).
- **Postgres** (Neon, `POSTGRES_DSN`) is reachable (`scripts/check_services.py`), but nothing in
  `src/` talks to it yet.

## Step 1: conversation state (`services/conversation.py`)

- Add `get_json(key)`, `set_json(key, value, ttl_s)` and `delete(key)` to `redis_store`. It stays
  the only Redis module.
- `conversation.py` owns `conversation:{user_id}` as a dataclass `Conversation`:
  - `category_id`
  - `fields` (collected so far)
  - `missing`
  - `original_query`
  - `follow_ups` (int)
  - `retrieval_confidence`
- Functions: `load(user_id)`, `save(conv)` with TTL `conversation_ttl_s` (new setting, 1800), and
  `clear(user_id)`.
- **Redis down means no conversation.** Log a warning and handle the message as a fresh one.
  `/chat` keeps working; the user only loses multi-turn memory.

## Step 2: the slot-filling flow (`services/pipeline.py::handle_message`)

At the start, `load()` the conversation. If one is open:

1. **Cancel words** ("cancel", "never mind", "stop"): clear the state and reply that it's cancelled.
2. **Re-extract** with the unchanged `extraction.extract_fields(query, category)`, run on
   `original_query + "\n" + the follow-ups`, so "till 11 PM" has its context. Merge into `fields`,
   keeping values already collected.
3. **Nothing new extracted, and `signals.classify()` says it's a new action, question or status
   ask:** the user moved on. Clear the state and run the normal pipeline on this message.
4. **Fields complete:** clear the state, then raise the SR, with the duplicate check from step 4.
5. **Still missing, and `follow_ups < 2`:** increment the counter, save, and ask again for only the
   missing fields (the existing `_join` / `_readable` wording).
6. **Still missing after 2 follow-ups:** clear the state, `ESCALATE`, and record it (step 3). This
   is F4's cap.

When a fresh message decides `COLLECT_DETAILS`, save a new conversation with `follow_ups=0`.

**Rule 6 must still apply to the merged fields.** A prod restart needs approval, and the
environment may arrive only in the follow-up.
- Refactor `RuleEngine._sr_path` (`services/decision/engine.py`) so the SR output is built from a
  category plus an `Extraction`.
- Add a public `async complete_sr(category_id, fields) -> DecisionOutput` that applies
  `_tighten`, and add it to the `DecisionEngine` protocol.

Every follow-up still logs one `querydesk.decision` line, with a `turn` field (F10).

## Step 3: `sr_requests` in Postgres (new owner module `services/state/postgres_store.py`)

- An `asyncpg` pool from `settings.postgres_dsn`. It raises `PostgresStoreError(UpstreamError)`
  (`core/errors.py`). `main.py` closes the pool on shutdown.
- Functions: `record_sr(...)` and `record_escalation(...)`.
- **`migrations/001_sr_requests.sql`**, using the PRD's columns:
  - `id`, `ticket_id`, `user_id`, `category_id`, `details` jsonb, `status`
  - `resolution_type` with a check constraint (`RAG` / `AUTO_SR` / `MANUAL_ESCALATION`)
  - `requires_approval`, `retrieval_confidence`, `last_notified_status`
  - `created_at`, `updated_at`
  - an index on `(user_id, category_id, status)`
- **`scripts/migrate.py`** applies `migrations/*.sql` in order and tracks them in a
  `schema_migrations` table. Plain SQL: no Alembic, no ORM.
- **What gets recorded:**
  - every raised SR (`AUTO_SR`, with the ticket id and status from the ticketing response)
  - every escalation (`MANUAL_ESCALATION`, no ticket id, status `ESCALATED`)

  RAG answers aren't recorded: they're not requests, and they belong in `decision_logs` later.
- **Failure handling:**
  - SR created but the Postgres write fails: log the error. The user still gets the SR number,
    because the ticketing service is the source of truth.
  - Escalation write fails: fall back to today's "contact the service desk directly" reply.
  - Escalation write succeeds: reply "I've flagged this for the service desk team".

## Step 4: duplicate check, F5 (`services/ticketing/client.py`)

- Add `list_open_srs(user_id) -> list[SRRecord]`: `GET /sr?user_id=…`, filtered to non-terminal
  statuses with the existing `TERMINAL_STATUSES`. The stand-in (`tests/fakes/fake_ticketing.py`)
  already supports this filter.
- **Before creating an SR:** if this user already has an open SR in the same category, reply "You
  already have SR {id} open for {category} ({status})" and create nothing.
- The idempotency key stays as the backstop for retries.
- Field names follow the PRD. Re-check them against the OpenAPI file once it lands in
  `querydesk-docs`.

## Tests (offline)

`fakeredis` for conversations, a monkeypatched in-memory `postgres_store`, and the stand-in
ticketing app in-process, as `tests/integration/test_chat_pipeline.py` already does.

- **The acceptance flow.** "keep UAT-2 up for release testing" → COLLECT_DETAILS (asks for extend
  until) → "till 11 PM" → RAISE_SR with all three fields, approval noted.
- Only missing fields are asked for; already-collected values aren't overwritten.
- Two follow-ups with fields still missing → ESCALATE, recorded, with no SR created.
- "cancel" clears the state.
- A new question mid-conversation is answered normally and clears the state.
- A prod environment arriving in the follow-up still triggers rule-6 approval.
- **Duplicate:**
  - an open SR in the same category gives the "already open" reply, with no second SR
  - a closed one allows a new SR
- Redis down → single-turn behaviour, no 500.
- Postgres down → the SR is still returned; an escalation falls back to "contact the desk".
- Unit tests for each new store module's error mapping.

## Files

- **New:**
  - `src/querydesk/services/conversation.py`
  - `src/querydesk/services/state/postgres_store.py`
  - `migrations/001_sr_requests.sql`
  - `scripts/migrate.py`
  - `tests/unit/test_conversation.py`
  - `tests/unit/test_postgres_store.py`
- **Edit:**
  - `services/pipeline.py`
  - `services/decision/engine.py`
  - `services/state/redis_store.py`
  - `services/ticketing/client.py`
  - `core/config.py`
  - `core/errors.py`
  - `main.py`
  - `tests/integration/test_chat_pipeline.py`
  - `CLAUDE.md`, `README.md`, `docs/task.md`
- **Reuse:**
  - `extraction.extract_fields`
  - `signals.classify`
  - `catalog.get`
  - `ticketing.idempotency_key` / `create_sr` / `TERMINAL_STATUSES`
  - `pipeline._join` / `_readable`

## Not in Day 6

`decision_logs` in Postgres, the webhook receiver and notifications (Day 7), STATUS_LOOKUP
answers (Day 8), and the bge-base embedding trial (`docs/latency-recommendation.md`). Jev spends
no credits (`docs/jev-notes.md`).

## Verification

- `uv run pytest -q`, `ruff check`, `ruff format --check` green; CI green on the PR.
- `uv run scripts/migrate.py` against Neon creates the table; a second run is a no-op.
- **Live acceptance**, using the stand-in ticketing on port 8081 plus the app:
  1. `POST /chat` "keep UAT-2 up for release testing" → asks for the missing field(s).
  2. `POST /chat` "till 11 PM" → returns an `sr_id`, with an approval note.
  3. Resend the first message → "already open" reply.
  4. The `sr_requests` row is in Neon.
  5. An out-of-scope message → "flagged" reply, with a `MANUAL_ESCALATION` row.
