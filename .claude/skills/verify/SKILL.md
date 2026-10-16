---
name: verify
description: Run the orchestrator against the stand-in ticketing service and drive POST /chat to observe a change live.
---

# Verify the orchestrator live

Uses the real Fireworks / Qdrant / Upstash / Neon from `.env` (costs Fireworks calls, never Jev).

## Launch (pick free ports; 8000/8081 may be in use)

```bash
uv run uvicorn tests.fakes.fake_ticketing:app --port 8091 &
TICKETING_BASE_URL=http://127.0.0.1:8091 uv run uvicorn querydesk.main:app --port 8097 &
# degraded-store variants, same ticketing:
TICKETING_BASE_URL=http://127.0.0.1:8091 POSTGRES_DSN=postgresql://nobody:x@127.0.0.1:1/none uv run uvicorn querydesk.main:app --port 8098 &
TICKETING_BASE_URL=http://127.0.0.1:8091 REDIS_URL=redis://127.0.0.1:1 uv run uvicorn querydesk.main:app --port 8099 &
```

Status notifications (Day 7) — same `WEBHOOK_SECRET` in all three, and point the stand-in at the
app:

```bash
export WEBHOOK_SECRET=dev-secret
STAND_IN_WEBHOOK_URL=http://127.0.0.1:8097/webhooks/sr-status uv run uvicorn tests.fakes.fake_ticketing:app --port 8091 &
TICKETING_BASE_URL=http://127.0.0.1:8091 uv run celery -A querydesk.worker worker --pool=solo -l info --without-gossip --without-mingle --without-heartbeat &
# optional, fast reconcile for a test run:
RECONCILE_INTERVAL_S=15 TICKETING_BASE_URL=http://127.0.0.1:8091 uv run celery -A querydesk.worker beat -l info -s "$CLAUDE_JOB_DIR/tmp/beat-schedule" &
```

Ready when `curl localhost:8097/health` returns `{"status":"ok"}` (~10–20 s: Qdrant check at boot).

## Drive

- `POST /chat` with `{"user_id": "...", "query": "..."}`. Use a fresh `user_id` per flow
  (e.g. suffix `$(date +%s)`): conversations live in Redis for 30 min and the stand-in keeps SRs
  in memory per process.
- The app logs one `querydesk.decision` JSON line per message: `turn` (0 = fresh), `rule`,
  `router_top_two`. Read it to see *which path* produced a reply; replies alone can hide it
  (a fresh-message rule-5 escalation looks identical to an F4-cap escalation).
- Conversation state: `conversation.load(user_id)` via `uv run python -c ...`.
- `sr_requests` rows: query Neon with asyncpg using `settings.postgres_dsn`; filter on your user ids.
- Stand-in SRs: `curl "localhost:8091/sr?user_id=..."`; move status with `PATCH /sr/{id}/status`.

## Flows worth driving

- Slot filling: "keep UAT-2 up for release testing" → "till 11 PM"; then resend the first message.
- Vague start: "I need my UAT extended please" → "it's UAT-3" → "because of regression testing".
- Rule 6 across turns: "restart the claims app please" → "prod".
- Cancel inside vs. outside a conversation; "stop the claims app" must not count as a cancel.
- Unhelpful replies: "keep UAT-2 up for release testing" → "not sure" → "hmm" → escalation that
  names the missing field, with a row carrying `category_id` and the collected fields.

Follow-ups are extracted from the user's reply only and every value must appear in its source
text; read the Redis state after each turn to see exactly what was collected — the reply text
alone doesn't show it.

- Notifications: raise an approval SR, `PATCH /sr/{id}/status` to APPROVED on the stand-in, then
  read `notifications` and `sr_requests.last_notified_status` for the user (Neon). The worker logs
  one `sr_id=… status=… source=webhook|reconcile written=True|False` line per change.
- Lost webhook: stop the app, PATCH, then `uv run celery -A querydesk.worker call querydesk.reconcile`.
- Replay: resend an identical signed body → 202 and `written=False`; a wrong signature → 401.

## Gotchas

- Bash heredocs containing apostrophes break in this harness; write helper scripts with the Write
  tool into `$CLAUDE_JOB_DIR/tmp` instead.
- `grep -i error` on the app log matches the `"error": null` key in every decision line.
- Kill servers on Windows: `Get-NetTCPConnection -LocalPort <p> -State Listen | % { Stop-Process -Id $_.OwningProcess -Force }`.
- The served stand-in numbers SRs from the clock (e.g. 24930355), not 1001: notifications are
  unique per `(sr_id, status)`, so a reused id's status changes would be dropped as duplicates.
- Upstash `INFO stats` counters flip between shards; don't use them to measure Celery's idle cost.
- Killing the worker by port doesn't work; stop the `celery` python processes by command line.
- Live runs leave rows in Neon `sr_requests` and `notifications`; use recognisable user ids so they can be deleted.
