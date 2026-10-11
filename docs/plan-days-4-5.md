# Plan — Days 4 and 5 (orchestrator lane)

Written 2026-10-11, after Days 1–3 merged (PR #1). Scope is the orchestrator lane only. The
partner's ticketing service doesn't exist yet, so Day 4 is built and tested against a **local
stand-in** that implements the PRD's v1.1 contract (`tests/fakes/fake_ticketing.py`). When the real
service lands, only `TICKETING_BASE_URL` changes.

Progress is tracked in [task.md](task.md); this file explains the *what* and *why*.

---

## The problem Day 4 solves

Today `/chat` answers everything from the knowledge base. The PRD wants each message routed to one
of six decisions. Eval v0 showed why that needs more than a retrieval score: **83% of action
requests score above `confidence_high`**. "Restart the claims app in UAT" matches the restart
article almost as well as "does a restart cause downtime?". So the decision layer needs a separate
**"is this an action?"** signal, and an action must never be answered with an article. That is the
PRD's most important failure mode.

**Decision (2026-10-11):** action and status detection is **rule-based**. It is deterministic,
takes about 0 ms, and is measured on the eval set. The LLM is called **only on the SR path**, to
extract required fields, so the answer path gets no extra call. Jev may replace the signal on Day 5.

---

## Day 4 — decision layer

**Done when:**
- a `/chat` message creates an SR in the (stand-in) ticketing service
- resending the same message returns the **same** SR
- routing accuracy ≥ 90% and action requests answered with an article ≤ 5% on the eval set

### Step 1: Finish Day 3 (grow and re-calibrate the eval set)
- Add ~20 rows:
  - near-miss out-of-scope (HR, payroll, laptop, VPN, printer)
  - questions without "?"
  - requests phrased as questions ("can you restart pas-web in UAT?")
  - problem statements
  - question-shaped informational rows ("can I restart UAT myself?")
- `test_eval_set.py` checks PRD **minimums** (≥ 30/20/10 and ≥ 3 action / ≥ 2 informational per
  category) instead of exact counts.
- Re-run the eval. A proposed threshold moving by more than 0.03 needs sign-off before config
  changes.

### Step 2: Plumbing
- `rag.retrieve()` returns hits and the dense score; `rag.generate()` produces the answer.
  - The decision needs the score *before* choosing to generate.
  - `answer()` stays as a wrapper.
  - The no-hits → no-LLM guardrail stays in `generate()`.
- `ResolutionType.AUTO_SR` is added, and the `Decision` enum moves to `models/schemas.py`.
- `services/catalog.py` is the one reader of `categories.json` in app code (invariant #3).

### Step 3: Decision signals (`services/decision/`)
- `signals.classify(text) -> Intent(is_action, is_status)` is pure pattern matching:
  - **status**: asks about an existing request or SR
  - **question**: ends in "?" or starts with what / how / is / does / can I …
  - **request marker**: please / pls / can you / I need … or an imperative verb at the start
  - **action** = a request marker, **or** not a question. This turns problem statements like "I
    can't see my leads" into actions.
- `extraction.extract_fields(query, category)` makes one LLM call returning JSON for the
  category's `required_fields`.
  - Absent fields are `null`, never guessed.
  - Garbage output or a Fireworks failure is treated as *all fields missing*, so the user is asked;
    the system never guesses.
  - Categories with no required fields skip the call.

### Step 4: Rule engine (`services/decision/engine.py`)
- A `DecisionEngine` interface (`async decide(input) -> output`) is selected by the
  `decision_engine` setting, so Jev drops in on Day 5.
- The PRD's six rules, first match wins:

| # | When | Decision | resolution_type |
| --- | --- | --- | --- |
| 1 | asks about an existing request | STATUS_LOOKUP | MANUAL_ESCALATION (lookup is Day 8) |
| 2 | not an action, has hits, score ≥ high | ANSWER | RAG |
| 3 | not an action, has hits, score ≥ mid | ANSWER_WITH_DISCLAIMER | RAG |
| 4 | action and router confident | RAISE_SR if fields complete, else COLLECT_DETAILS | AUTO_SR |
| 5 | anything else | ESCALATE | MANUAL_ESCALATION |
| 6 | applied last, **tighten only** | force approval / force escalation (SR path only) | — |

- **Rule 4 deviates from the PRD on purpose.** The PRD reads "an action, *or a score below mid*,
  with a confident router", which let a plain question with a weak KB match open an SR the user
  never asked for. A question that falls through rules 2–3 now escalates instead (decided
  2026-10-11 after the Day 4 code review).

- Rule 6 lives in `data/decision_rules.json`, which the orchestrator owns.
  - Example: a production restart needs approval even though `it_application_restart` is
    `conditional`.
  - It can turn approval *on* or force an escalation, never the reverse.
- `requires_approval` comes from the catalog: `always` → yes, `none` → no, `conditional` → no
  unless a rule-6 match.

### Step 5: Ticketing client and the stand-in service
- `services/ticketing/client.py` is the only module that talks to ticketing.
  - `create_sr()` sends `POST /sr` with an `Idempotency-Key` header; `get_sr()` reads one.
  - Failures raise `TicketingError(UpstreamError)`, so they become 502 with no route change.
- **Idempotency key** = sha256(user, category, normalised fields, UTC date).
  - A retried or double-sent message the same day returns the original SR.
  - A legitimate repeat on another day gets a new one. UAT extensions "do not repeat" per the KB.
- `tests/fakes/fake_ticketing.py` is the stand-in, in memory, per PRD v1.1:
  - an unknown category → 400
  - a repeated key → 200 with the original
  - new SRs start in `PENDING`

### Step 6: Wire `/chat` and log decisions
- `services/pipeline.handle_message()`:
  1. embed once
  2. retrieve and route concurrently
  3. classify
  4. decide
  5. act — generate an answer, create the SR, ask for the missing fields, or escalate
- `ChatResponse` gains `decision`, `category_id`, `sr_id`, `missing_fields` and
  `requires_approval`.
- Every decision is logged as one JSON line (PRD F10) with its inputs, outputs, engine and
  latencies. It moves to the `decision_logs` table on Day 6.

### Step 7: Measure
- The eval gains:
  - **routing accuracy**: decision matches the label; target ≥ 90%
  - **action requests answered with an article**: target ≤ 5%
  - a confusion table
  - decision latency (target < 500 ms excluding the LLM extraction, which is reported separately)
- Written to `docs/eval-report-v1-day4.md`. If a target misses, iterate on the signal rules and
  record each iteration.

### Step 8: Live end-to-end (stand-in Checkpoint 1)
With the stand-in on :8081:

| Message | Expected |
| --- | --- |
| "restart the claims app in UAT, it's hung" (sent twice) | RAISE_SR, same SR id both times |
| "app is frozen pls restart" | COLLECT_DETAILS: application, environment |
| "does a rolling restart cause downtime?" | ANSWER with a citation |
| "please bounce agent-portal in prod" | RAISE_SR, `requires_approval` true (rule 6) |
| "what is the capital of Peru?" | ESCALATE |

### Step 9: Wrap-up
CLAUDE.md conventions, `.env.example`, the task board, and a PR with CI green.

---

## Day 5 — Jev, and the latency decision

**Done when:**
- routing accuracy is reported for both engines on the same eval set, and the better one is the
  default
- a written recommendation exists for the embedding-latency risk

1. **Jev Playground session (you, needs your TypeSafe account).** Confirm the real request and
   response for the three questions the PRD gives Jev:
   - is this an action request?
   - is it about an existing request?
   - does the top article answer it (as a probability)?

   Record them in `docs/jev-notes.md`. Everything below waits on this.
2. `services/llm/jev.py` is the only module that talks to TypeSafe and raises
   `JevError(UpstreamError)`. `TYPESAFE_API_KEY` is first read here.
3. `JevEngine` implements `DecisionEngine`.
   - Jev's three answers replace the rule-based `Intent`, and its probability is used alongside the
     dense score.
   - On a Jev error, or a response slower than ~400 ms, it falls back to `RuleEngine` and logs
     `engine=rules-fallback`. The decision layer budget is < 500 ms.
4. `eval_run.py --engine both`. The report puts the two engines side by side:
   - routing accuracy
   - action requests answered with an article
   - p50/p95 latency
   - the rows where they disagree

   The higher routing accuracy becomes the default `decision_engine`.
5. **Latency spike** for the Day 3 risk. Dense embedding takes 1.6–2.2 s per query, so the answer
   path can't meet p95 < 3 s.
   - Time a local fastembed dense model against `qwen3-embedding-8b` on the eval set.
   - Compare retrieval hit@1 and router accuracy as well as speed.
   - Write a recommendation before Day 8. Switching models means re-indexing and re-calibrating.

### Day 5 risks
- **Jev's request shape differs from the PRD's assumptions.** That's why the rule engine ships
  first: the interface is fixed, so a different shape changes only `jev.py`.
- **Jev is slower than the 500 ms decision budget.** The fallback keeps every message working; the
  report shows how often it was used.
