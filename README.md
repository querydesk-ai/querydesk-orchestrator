# SR Resolution Chatbot — Phase 0

Scaffold only. No RAG, no retrieval, no decision layer yet — this phase
just proves FastAPI boots and the Fireworks connection works end-to-end.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env, fill in FIREWORKS_API_KEY at minimum
```

## Run

```bash
uvicorn app.main:app --reload
```

## Verify it works

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test","query":"hello"}'
# {"reply":"...","resolution_type":"STUB"}
```

If you get a 502 with a message about `FIREWORKS_API_KEY not set`, your
`.env` isn't being picked up — check it's in the project root, not inside
`app/`.

## What's in here

```
app/
  config.py              # all settings, read from .env — nothing else touches os.environ
  main.py                # FastAPI app + /health
  routers/chat.py         # POST /chat — stub, just forwards to Fireworks for now
  services/fireworks_client.py   # chat + embeddings, used by everything downstream
  models/schemas.py       # request/response shapes
```

## Next (Phase 1)

- Qdrant collection for knowledge-base chunks
- Real RAG retrieval + grounded answer generation
- Replace the STUB resolution_type with the real decision pipeline
