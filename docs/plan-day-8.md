# Day 8 plan: status questions, notifications endpoint, chat page

**Done when:** a requester can ask about their SRs and see status-change messages on one page (PRD
user stories 4–5, F9). WebSocket push stays out of scope (PRD "later"); the page polls.

## What was built

- **Status answers** (`services/status.py`, rule 1):
  - "status of my requests" lists the user's open SRs, newest first, with category names and
    readable statuses, capped at 10 ("…and N more"). With none open: "You have no open requests."
  - "any update on SR-1042" reports that one SR, closed ones included. The SR number is parsed by
    `signals.sr_number()`.
  - Another user's SR reads **exactly** like a missing one ("I can't find SR … among your
    requests"), so numbers can't be probed.
- **Status questions skip the search.** Rule 1 needs only `classify().is_status`, so
  `_decide_fresh` decides before embedding: no Fireworks call, no Qdrant query. The decision log
  shows `embed: 0.0`, `top_dense_score: null` and no router. `_Fresh.retrieval`/`router` are
  `None` for these turns.
- `ticketing.get_sr()` returns `None` on a 404 instead of raising, so "not found" isn't a 502.
- **Notifications API** (`api/routes/notifications.py`):
  - `GET /notifications?user_id&unread_only&limit` returns newest first. `limit` is 1–100.
  - `POST /notifications/read {user_id, ids}` returns `{marked}`, scoped by `user_id`.
  - A store failure is a 502.
  - No login in v1: `user_id` is a parameter, as on `/chat`.
- **Chat page** (`GET /`, `src/querydesk/static/index.html`):
  - One file, vanilla JS, no CDN or build step.
  - The user id is remembered in `localStorage`. Each reply shows decision/SR/approval badges,
    and citations link only to http(s) URLs.
  - It polls unread notifications every 10 s, shows each one once, then marks them read.
  - All text goes through `textContent`. Light and dark themes come from `prefers-color-scheme`.
  - The page ships as package data.
- **Eval:**
  - A new `status` kind (expects `STATUS_LOOKUP`, no category) and 6 rows (`sts-01..06`).
  - Status rows count toward routing accuracy and get their own line, but stay out of threshold
    calibration.
  - The near-misses ("Please close SR-1042", "what does status Pending mean") are already covered
    in `tests/unit/test_signals.py`.

## Eval (2026-10-17, 94 rows, thresholds unchanged)

| Metric | Result | Target |
| --- | --- | --- |
| Routing accuracy | **96%** (strict 94%) | ≥ 90% |
| Action requests answered with an article | **0%** | ≤ 5% |
| SR-bound category accuracy | 100% | ≥ 90% |
| Informational source hit@k | 100% | — |
| Status rows | 6/6 `STATUS_LOOKUP` | — |

The six misses are the same as on 2026-10-15: inf-02 and inf-19 got a disclaimer (lenient pass),
inf-24 escalated, oos-03 and oos-14 were answered with a disclaimer, and oos-18 was routed. The
report proposes `confidence_mid` 0.49 → 0.56 and `router_min_score` 0.46 → 0.48. **Not applied.**
Invariant #4 needs a deliberate decision, and a mid of 0.56 would push error-code lookups (PWD-1407
scores 0.497) out of the answer band. Revisit with the bge-base trial, which re-calibrates anyway.

## Verified live (2026-10-17, stand-in ticketing, Fireworks, Upstash, Neon, Chrome)

- **Raise from the page:** "keep UAT-2 up till 11 PM for release testing" raised SR 24995570, with
  `raise sr` / `SR …` / `needs approval` badges.
- **Status question:** "status of my requests" gave "You have 1 open request: SR 24995570: UAT
  extension past 9 PM, Pending". The log shows `embed: 0.0`, `top_dense_score: null`, 8.8 ms total.
- **Notification:** after a PATCH to APPROVED, the approval bubble appeared within one poll. It was
  written at 07:14:21 and marked read at 07:14:29, and the unread list then came back empty.
- **Single SR:** "any update on SR-24995570?" (owner) returned Approved. Another user asking about
  the same SR got "I can't find SR 24995570 among your requests".
- No console errors.
- **Phone width** (second `/verify` run, the page in a 390 px iframe because resizing a maximised
  window does nothing): no horizontal scroll (`scrollWidth` 390), bubbles wrap, the header fits on
  one line. Only the dark theme was seen (the machine is in dark mode).
- **Regressions and failures** (second run): slot filling, "already open" on a resend, cancel
  inside/outside a conversation, "stop the claims app" not read as a cancel, and an informational
  answer still searching were all unchanged. A status question mid-conversation clears the
  conversation. Ticketing down → a status question is a 502. Postgres down → both notification
  routes are 502. A missing `user_id` or `limit=0` → 422.
