# Deploying the orchestrator to AWS (one EC2 box, Docker Compose, Caddy HTTPS)

A first-timer runbook for a single EC2 instance running the api, worker, beat, Redis and (until the
real ticketing service is deployed) the stand-in ticketing service, behind Caddy, which gets a
Let's Encrypt certificate by itself. Qdrant Cloud, Neon and Fireworks stay external. For the
ticketing team's full-stack deploy see [docker.md](docker.md).

```
Internet -> :443 Caddy -> api:8000     (worker, beat, redis, ticketing stand-in: internal only)
```

## 0. Spend credits only

This deployment is meant to run entirely on AWS credits.

- Billing -> **Credits**: check the balance and the expiry date before creating anything.
- Billing -> **Budgets**: add a low alert (e.g. $5) on actual and forecasted cost, and make sure the
  budget is not netting credits out, or it will never fire.
- Use only one EC2 t3.medium, one gp3 volume and one Elastic IP. Don't add a NAT Gateway, load
  balancer, RDS, Route 53 domain or Marketplace AMI.
- **Stop the instance when you don't need it.** A stopped instance still bills its disk and Elastic IP.
- Teardown: terminate the instance, release the Elastic IP, delete the volume and snapshots.

## 1. Account safety (once)

1. Turn on MFA for the root user; create an IAM admin user with MFA and use that day to day.
2. Pick one region and stay in it.

## 2. Launch the instance (console)

- EC2 -> Launch instance: **Ubuntu 24.04 LTS**, **t3.medium**, 30 GB gp3.
- Create a new key pair (ED25519 `.pem`); never commit it.
- Security group, inbound only: **22 from My IP**, **80** and **443** from anywhere. Nothing else.
- Allocate an **Elastic IP** and associate it with the instance.

## 3. Prepare the server

```bash
ssh -i querydesk.pem ubuntu@<elastic-ip>
sudo apt update && sudo apt upgrade -y
# Docker Engine + compose plugin: follow Docker's "Install on Ubuntu" apt-repository steps.
sudo usermod -aG docker ubuntu     # then log out and back in
# 2 GB swap: the image build is memory-heavy on a 4 GB box
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
git clone https://github.com/querydesk-ai/querydesk-orchestrator.git && cd querydesk-orchestrator
git checkout v0.9.0
```

## 4. DNS

Create a free name at duckdns.org and point it at the Elastic IP. From your laptop,
`nslookup <name>.duckdns.org` must return that IP before you start Caddy.

## 5. Secrets

```bash
cp .env.example .env && chmod 600 .env && nano .env
```

Fill in `FIREWORKS_API_KEY`, `QDRANT_*`, `POSTGRES_DSN`, `WEBHOOK_SECRET`, `WHATSAPP_ID_SECRET`
(use the existing value to keep WhatsApp user ids), `META_*`, and `DOMAIN=<name>.duckdns.org`.
`REDIS_URL` and `TICKETING_BASE_URL` are overridden by compose. If Neon or Qdrant has an IP
allowlist, add the Elastic IP. Never commit `.env` or paste it anywhere.

## 6. Start

```bash
alias dc='docker compose -f compose.yaml -f compose.prod.yaml --profile stand-in'
dc build
dc run --rm api python scripts/migrate.py --status   # look first
dc run --rm api python scripts/migrate.py
dc up -d
dc ps && dc logs -f caddy api
```

The knowledge base is already indexed in Qdrant Cloud; don't run `ingest.py` unless it is missing.

## 7. Verify

- `curl https://<name>.duckdns.org/health` and `/health/ready` return 200.
- Open the chat page, ask a KB question, and check the answer cites an article.
- Ask for an action ("reset my VPN access"); an SR is raised. Approve it on the stand-in via an SSH
  tunnel (`ssh -L 8081:localhost:8081 ...`, then http://localhost:8081/docs). The notification should
  show up on the chat page within ~10 s.
- `dc logs worker beat`: no Redis/auth errors, exactly one beat.
- `curl http://<elastic-ip>:8000` from your laptop fails.
- `sudo reboot`, then confirm everything comes back on its own.

## 8. WhatsApp (optional)

Set Meta's webhook callback to `https://<name>.duckdns.org/webhooks/whatsapp` with
`META_VERIFY_TOKEN`; see [whatsapp.md](whatsapp.md).

## Operate

- **Update:** `git fetch --tags && git checkout <tag>`, `dc build`, `dc up -d`.
- **Rollback:** check out the previous tag, `dc build`, `dc up -d`. Migrations are forward-only: only
  roll back to a version that works with the schema already applied.
- **Memory limits** in `compose.prod.yaml` are starting values; watch `docker stats` and adjust.
- **Wrap-up:** rotate every API key, then tear down (section 0).
