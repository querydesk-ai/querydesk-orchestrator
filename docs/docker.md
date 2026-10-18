# Running the orchestrator in Docker

This note is for the ticketing team's full-stack Compose and Nginx on the Day 10 EC2 box. The
orchestrator's own `compose.yaml` runs its stack for local work and the demo. It is not the
deployment file.

## The image

`docker build --target runtime -t querydesk-orchestrator .`

One image runs all three orchestrator processes. Only the command differs:

| Process | Command | Port |
| --- | --- | --- |
| api | the default `CMD` (uvicorn on `0.0.0.0:8000`, `--proxy-headers`) | 8000 |
| worker | `celery -A querydesk.worker worker --concurrency=1 -l info --without-gossip --without-mingle --without-heartbeat` | none |
| beat | `celery -A querydesk.worker beat -l info -s /tmp/celerybeat-schedule` | none |

- **Run exactly one beat**, or reconciliation runs twice. The worker can be scaled, but keep
  `--concurrency=1` per worker.
- **Health:** the image's `HEALTHCHECK` calls `/health` (the process is up). `/health/ready` also
  checks Qdrant. Disable the healthcheck for the worker and beat, which serve no HTTP.
- **The image runs as a non-root user** (uid 10001).
- **The BM25 model is in the image.** Nothing is downloaded at start.

## Nginx

Proxy everything to the `api` service on port 8000:

- `/`: the chat page.
- `/chat`, `/notifications`: what the page calls.
- `/webhooks/sr-status`: the ticketing service's status webhooks. If the ticketing service is on
  the same Compose network, it can post to `http://api:8000/webhooks/sr-status` directly,
  without going through Nginx.
- `/webhooks/whatsapp`: Twilio's inbound messages, once that PR lands.
- `/docs`: Swagger. Leave it out of the public site if you prefer.

Set `proxy_read_timeout` to at least 30 s. A chat answer takes 5–8 s end to end.

uvicorn trusts `X-Forwarded-*` headers only from `127.0.0.1` by default. When Nginx runs in
another container, set `FORWARDED_ALLOW_IPS` on the api to Nginx's address, or to `*` if port
8000 isn't published outside the Compose network.

## Environment

Every orchestrator process reads the same variables. `.env.example` lists them.

| Variable | Notes |
| --- | --- |
| `FIREWORKS_API_KEY` | LLM and embeddings. Required. |
| `QDRANT_URL`, `QDRANT_API_KEY` | Qdrant Cloud, already indexed. |
| `POSTGRES_DSN` | The orchestrator's own Neon database (`sr_orchestrator`). It is not the ticketing database. |
| `REDIS_URL` | The query cache, conversations and the Celery broker. Use a Redis container (`redis://redis:6379/0`) rather than Upstash: Celery's broker traffic would use up the free tier. |
| `TICKETING_BASE_URL` | The ticketing service as the orchestrator reaches it, e.g. `http://ticketing:8080`. |
| `WEBHOOK_SECRET` | The same value as the ticketing service's signing secret. |

Run the migrations once against the orchestrator's database before the first start:

```bash
docker compose run --rm api python scripts/migrate.py
```

The orchestrator never connects to the ticketing service's database, only to its API.
