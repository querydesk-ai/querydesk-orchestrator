# Plan: a universal ticketing connector

**Status:** approved 2026-10-29. Work starts **after the Day 10 demo**; nothing is built yet.

**Done when:** QueryDesk can raise and track SRs in ServiceNow, and later Jira Service Management, by
switching one setting. Adding another platform takes a configuration file, not new Python code.

## Why

QueryDesk raises SRs only in our own ticketing service (contract v1.1). Insurance IT teams usually run
ServiceNow, and others run Jira Service Management, Zendesk or Freshservice. For QueryDesk to work as
an agent in front of any of them, the ticketing side has to be pluggable.

One adapter module per platform was considered and rejected: each new platform would mean new code,
new tests and new review. Every ticketing platform does the same four things over REST: create a
ticket, read one, list a user's open ones, and list the ones changed since a time. Each also pushes
status changes through a webhook. Only the details differ: URLs, auth, field names, JSON shapes,
pagination and status names. So the platform-specific part is **data**, and one engine interprets it.

The seam already exists. Only `services/ticketing/client.py` talks to the ticketing service, and only
`pipeline.py`, `status.py`, `notifications/notify.py` and `main.py` call it. Everything above that line
stays as it is: decisions, RAG, chat page, WhatsApp and notifications.

## Design: one engine plus one spec per platform

### 1. Connector spec: `data/connectors/<platform>.yaml`

Validated with pydantic at startup. A bad spec refuses to start.

- **`auth`**: `basic`, `bearer`, `oauth2_client_credentials` or `api_key_header`. It holds only the
  **names** of the settings that carry the secrets, never values.
- **`operations`**: `create`, `get`, `list_open`, `list_updated`. For each one:
  - `method` and `path` (templated, e.g. `/api/now/table/sc_req_item/{id}`)
  - `query` and `body` templates, rendered from the SR (`user`, `category`, `fields`, `description`,
    `idempotency_key`, `since`). Rendering uses a **sandboxed** Jinja2 environment, so a template
    can't reach Python.
  - `response`: JSONPath expressions for `id`, `status`, `category`, `user` and `updated_at`, plus
    `items` for lists.
  - `pagination`: `none`, `offset` (ServiceNow `sysparm_offset`), `page`, `start_at` (Jira),
    `cursor` or `link_header`.
- **`categories`**: maps each `category_id` to platform values (a catalog item `sys_id`, or a
  `requestTypeId`) and maps our required fields to platform field names.
- **`statuses`**: maps platform status values to our state machine (PENDING, APPROVED, REJECTED,
  IN_PROGRESS, RESOLVED, REVERTED).
- **`idempotency`**: either `header` (the platform honours a key, as our own service does) or
  `correlation_field` (write the key into a field such as ServiceNow `correlation_id`, then search it
  before creating).
- **`webhook`**: verification (`hmac_sha256` plus a header name, or `shared_header`), plus JSONPath for
  `sr_id`, `status` and `user` in the payload.

### 2. The engine: `services/ticketing/connector.py`

Generic. It loads the spec named by `settings.ticketing_connector`, renders requests, calls the
platform with httpx, extracts values with JSONPath, maps statuses, and returns the existing `SRRecord`.
Every failure raises `TicketingError`, so routers keep mapping it to 502 in one place. `client.py`
keeps its exact public API (`create_sr`, `get_sr`, `list_open_srs`, `list_updated_since`, `aclose`)
and delegates to the engine, so callers don't change.

### 3. Our own ticketing service becomes just another spec

`data/connectors/querydesk.yaml` describes contract v1.1: header idempotency, the paginated `GET /sr`
and our HMAC webhook. It's the default. **The existing suite passing unchanged against
`tests/fakes/fake_ticketing.py` is the proof that the engine is general.** The hand-written HTTP code
in `client.py` is then deleted.

### 4. One webhook route for every platform

`POST /webhooks/tickets/{connector}` verifies the request using the spec, extracts
`(sr_id, status, user)`, maps the status, and queues the existing `notify_status_change` task.
`handle_status_change()` stays the only write path, so the `(sr_id, status)` dedupe and the WhatsApp
fan-out come for free. `/webhooks/sr-status` stays as an alias for the querydesk spec. The beat
reconcile uses `list_updated` for every platform.

### 5. What stays in code: shared and platform-neutral

- **Validation against the catalog:** every category and required field is mapped, and every platform
  status maps to a known one. `data/categories.json` stays the only source of category ids, required
  fields and approval rules (invariant #3). It follows the same pattern as `decision/rules.py`.
- **Idempotency backup:** migration 005 adds a unique `idempotency_key` column to `sr_requests`. The
  engine claims the key before creating a ticket on platforms without header idempotency.
  `idempotency_key()` and `next_key()` are reused as they are.
- **Identity:** a `platform_users` table maps a `user_id` (web or `wa-…`) to the platform user, which
  templates reference as `{{ platform_user }}`. Unmapped users are raised by the service account, with
  the requester id in the description. Phone numbers still never leave `whatsapp_users`.
- **Escape hatch:** a spec may name one registered Python hook for one step, chosen from an allow-list
  in code and never loaded from the file. It covers quirks a template can't express, such as
  ServiceNow's two-step `order_now`. Use it rarely. Every hook is reviewed code.

### 6. Settings (`core/config.py` only)

`TICKETING_CONNECTOR` (default `querydesk`), plus the secret settings each spec names:
`SERVICENOW_INSTANCE_URL`, `SERVICENOW_USER`, `SERVICENOW_PASSWORD`, `JIRA_BASE_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`, and a webhook secret per connector. New dependencies are `jinja2` (sandboxed) and
`jsonpath-ng`.

## Order of work (one PR each)

1. Engine, spec schema and `querydesk.yaml`, with `client.py` delegating. The existing suite passes
   unchanged, and new unit tests cover templating, JSONPath, pagination styles and spec validation
   errors.
2. Migration 005, correlation-field idempotency, `platform_users`, and the universal webhook route.
3. `servicenow.yaml`, a respx fake of its Table and Catalog APIs, and offline tests.
4. `jira.yaml`. This should need no engine change; if it does, that shows where the design leaks.
5. `docs/connectors.md` (how to write a spec, and setup per platform), CLAUDE.md module ownership, and
   the decision recorded in querydesk-docs. The OpenAPI contract with the ticketing service doesn't
   change, because the native backend stays the default.

## Verification

- After PR 1, `uv run pytest -q` and `ruff` are green with **zero edits to existing tests**.
- Per spec, offline tests cover:
  - create, and a replay of the same key returning the same ticket
  - the open-SR duplicate check and status mapping
  - webhook signatures: good, bad and missing
  - reconcile
  - a broken spec (an unmapped category or unknown status) refusing to start
- Live: a free ServiceNow Personal Developer Instance. Set `TICKETING_CONNECTOR=servicenow` and raise
  "restart the claims app in UAT" in the chat page; the ticket appears in ServiceNow. Change its state
  there, and the notification appears on the chat page and on WhatsApp. "status of my requests" lists it.
  Jira Service Management's free cloud plan serves the same purpose for PR 4.
