from fastapi import FastAPI

from app.routers import chat

app = FastAPI(title="SR Resolution Chatbot", version="0.1.0-phase0")

app.include_router(chat.router)


@app.get("/health")
async def health():
    """Confirms the app is up. Doesn't check downstream deps yet —
    that'll matter once Qdrant/Postgres/Redis are wired in."""
    return {"status": "ok"}
