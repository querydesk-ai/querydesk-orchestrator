# Day 7 plan: status notifications (Checkpoint 2)

**Done when:** an SR is approved in Swagger and its requester is notified within 1 minute (PRD
Checkpoint 2, user story 4: "a requester gets a message when an SR is approved, rejected, resolved
or reverted").

PRD requirements covered:
- **F9:** notify the user on each status change (status questions on demand are Day 8).
- **"No lost status change":** webhook retries (the ticketing service's outbox) plus a reconciliation
  poll.
- **The orchestrator's `notifications` table.**

## Flow

```
ticketing PATCH /sr/{id}/status
  └─ signed POST /webhooks/sr-status ── 401 bad signature / 422 bad body / 503 broker down
       └─ Celery notify_status_change ── retried on UpstreamError (backoff, max 5)
            └─ notify.handle_status_change ── one row per (sr_id, status), sr_requests moves forward
Celery beat, every 10 min
  └─ reconcile ── GET /sr?updated_since=<watermark − 2 min> ── same handle_status_change
```

## Decisions

- **Signature:** `X-Signature` = lowercase hex HMAC-SHA256 of the raw request body under
  `WEBHOOK_SECRET`, no `sha256=` prefix (`services/notifications/signing.py`). An empty secret
  rejects everything. No timestamp header: a replay is harmless because processing is idempotent.
  **This needs to be written into contract v1.1 in `querydesk-docs`.**
- **Idempotency:** `notifications` is `UNIQUE (sr_id, status)`. The PRD state machine never
  revisits a status, so a retried webhook, a replay and a reconciliation overlap all write nothing
  (`ON CONFLICT DO NOTHING`). `status` is an orchestrator-internal column, not a contract change.
- **Out of order:** `sr_requests.status` only moves to a status ranked higher
  (`ticketing.STATUS_RANK`), so a late APPROVED after IN_PROGRESS is still notified but does not set
  the SR back.
- **PENDING is never notified:** the user was told in chat. `record_sr()` now sets
  `last_notified_status` to the creation status.
- **Category name:** the webhook carries no category, so `notify` asks `GET /sr/{id}`; if that
  fails the message just omits the name. Messages are fixed templates, no LLM.
- **Unknown SRs** (no `sr_requests` row, e.g. a failed Day 6 write) are still notified: the user id
  comes from the event.
- **The webhook route does no DB work.** It verifies, validates and publishes (off the event loop,
  publish retries capped so a dead broker answers 503 in about 5 s, not a hang).
- **Reconciliation** keeps a watermark in Redis (`reconcile:since`): the newest `updated_at` seen,
  never moving backwards; no watermark (or Redis down) → look back 24 h. It only sees an SR's
  *current* status, so a status skipped while webhooks were down is not notified. That's acceptable
  for a backstop.
- **One event loop per worker process** (`worker._run`): asyncpg pools and the httpx/Redis clients
  are loop-bound, so tasks never use `asyncio.run()`.
- **Celery 5.6.3 without the `[redis]` extra:** the extra caps redis-py below the pinned 8.1, and
  resolution fell back to Celery 5.3.1. kombu imports redis-py directly and works with 8.1.
- **Windows dev needs `--pool=solo`.**

## Verified live (2026-10-16, stand-in ticketing, Upstash, Neon)

- `/chat` "keep UAT-2 up till 11 PM for release testing" → SR 1001 with approval.
- PATCH → APPROVED at 12:52:58.5 UTC; notification row at 12:53:04.6: **about 6 s** (target < 60 s).
  `sr_requests` → APPROVED / last notified APPROVED.
- The same signed body sent again → 202, task returned `False`, no second row. A forged signature
  → 401.
- Orchestrator stopped, PATCH → IN_PROGRESS (all three of the stand-in's webhook attempts fail), then
  `celery call querydesk.reconcile` → IN_PROGRESS notified, `source=reconcile`.
- RESOLVED, REVERTED (SR 1001) and REJECTED with a note (SR 1002): one row each.
- Beat at a 15 s interval sent reconcile 5 times; each found 2 SRs and wrote nothing.
- **Upstash cost of an idle worker: not established.** `INFO stats` showed no increase with the
  worker idle, but Upstash's counter flips between shards and resets, so it can't be trusted. Read
  the daily command count on the Upstash dashboard over an idle hour before leaving a worker running.
  If it projects past the 500k/month quota, raise `broker_transport_options.polling_interval` or
  stop the worker when idle. The demo uses Redis on EC2.

## Found by /verify

- **SR ids must never be reused.** The served stand-in restarted its numbering at 1001, so after a
  restart an APPROVED webhook for a *new* SR 1001 collided with an earlier run's (1001, APPROVED)
  row and was dropped (`written=False`). The real service's ids are never reused, so the key stays
  as it is; the served stand-in now numbers from the clock. **Note for the contract:** SR ids are
  never reused, including across a ticketing database reset in dev (clear `notifications` if you
  reset it).
- A dead broker answers 503 in ~5 s (two publish retries plus the connect timeout).

## Not in Day 7

- `GET /notifications` and marking them read: Day 8.
- Contract v1.1 text for the webhook (above) in `querydesk-docs`.
- Re-check field names against the OpenAPI contract once it lands.
