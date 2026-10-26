"""
One inbound WhatsApp message -> the pipeline -> reply text. The pipeline and
Postgres are faked; what matters is the identity used, the reply shape, and
that a failure still answers the user.
"""

import pytest

from querydesk.core.config import settings
from querydesk.core.errors import LLMError, PostgresStoreError
from querydesk.models.schemas import Decision, ResolutionType
from querydesk.services.messaging import inbound, whatsapp
from querydesk.services.pipeline import ChatResult

PHONE = "+15551234567"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_id_secret", "id-secret")


@pytest.fixture
def pipeline(monkeypatch):
    seen = {"messages": [], "drained": 0}

    async def handle(user_id, query):
        seen["messages"].append((user_id, query))
        if "result" in seen:
            return seen["result"]
        raise seen["error"]

    async def drain():
        seen["drained"] += 1

    monkeypatch.setattr(inbound.pipeline, "handle_message", handle)
    monkeypatch.setattr(inbound.pipeline, "drain_decision_writes", drain)
    seen["result"] = ChatResult(
        reply="I've raised SR **1042** for Password reset.",
        resolution_type=ResolutionType.AUTO_SR,
        decision=Decision.RAISE_SR,
        sr_id="1042",
    )
    return seen


@pytest.fixture
def users(monkeypatch):
    rows = {}

    async def upsert(user_id, phone):
        rows[user_id] = phone

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://fake")
    monkeypatch.setattr(inbound.postgres_store, "upsert_whatsapp_user", upsert)
    return rows


@pytest.mark.anyio
async def test_the_pipeline_sees_the_pseudonymous_id_never_the_number(pipeline, users):
    reply = await inbound.reply_to(PHONE, "unlock my account")

    [(user_id, query)] = pipeline["messages"]
    assert user_id == whatsapp.user_id_for(PHONE) and "5551234567" not in user_id
    assert query == "unlock my account"
    assert users == {user_id: PHONE}
    assert reply == "I've raised SR *1042* for Password reset."
    assert pipeline["drained"] == 1


@pytest.mark.anyio
async def test_a_dependency_failure_still_answers(pipeline, users):
    del pipeline["result"]
    pipeline["error"] = LLMError("Fireworks timed out")

    assert await inbound.reply_to(PHONE, "hi") == whatsapp.ERROR_REPLY
    assert pipeline["drained"] == 1


@pytest.mark.anyio
async def test_an_unexpected_failure_still_answers(pipeline, users):
    del pipeline["result"]
    pipeline["error"] = KeyError("bug")

    assert await inbound.reply_to(PHONE, "hi") == whatsapp.ERROR_REPLY


@pytest.mark.anyio
async def test_a_failed_user_record_does_not_block_the_chat(pipeline, monkeypatch):
    async def broken(user_id, phone):
        raise PostgresStoreError("Neon down")

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://fake")
    monkeypatch.setattr(inbound.postgres_store, "upsert_whatsapp_user", broken)

    assert await inbound.reply_to(PHONE, "unlock my account") is not None
    assert len(pipeline["messages"]) == 1


@pytest.mark.anyio
async def test_no_postgres_means_no_user_record(pipeline, monkeypatch):
    async def must_not_run(user_id, phone):
        raise AssertionError("no POSTGRES_DSN: nothing to write to")

    monkeypatch.setattr(inbound.postgres_store, "upsert_whatsapp_user", must_not_run)

    assert await inbound.reply_to(PHONE, "unlock my account") is not None


@pytest.mark.anyio
async def test_media_without_text_gets_the_text_only_reply(pipeline):
    assert await inbound.reply_to(PHONE, "", has_media=True) == whatsapp.TEXT_ONLY_REPLY
    assert pipeline["messages"] == []


@pytest.mark.anyio
async def test_an_empty_message_gets_no_reply(pipeline):
    assert await inbound.reply_to(PHONE, "   ") is None
    assert pipeline["messages"] == []
