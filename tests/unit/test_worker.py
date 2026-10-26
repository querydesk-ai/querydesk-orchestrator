"""
Celery tasks, run directly (no broker): they validate the event and run the
async services on one long-lived loop per worker process.
"""

import asyncio

import pytest
from celery.exceptions import Retry
from pydantic import ValidationError

from querydesk import worker
from querydesk.core.errors import UpstreamError, WhatsAppError


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def handle(**kwargs):
        seen.append((kwargs, asyncio.get_running_loop()))
        return True

    monkeypatch.setattr(worker.notify, "handle_status_change", handle)
    monkeypatch.setattr(worker, "_loop", None)
    return seen


def test_notify_task_passes_the_event_through(calls):
    event = {"sr_id": "1001", "user_id": "u1", "to_status": "APPROVED", "note": "ok"}

    assert worker.notify_status_change.run(event) is True

    [(kwargs, _)] = calls
    assert kwargs == {"sr_id": "1001", "user_id": "u1", "to_status": "APPROVED", "note": "ok"}


def test_tasks_share_one_event_loop(calls):
    event = {"sr_id": "1", "user_id": "u1", "to_status": "APPROVED"}
    worker.notify_status_change.run(event)
    worker.notify_status_change.run({**event, "to_status": "IN_PROGRESS"})

    # asyncpg pools and httpx clients are bound to the loop that made them.
    assert calls[0][1] is calls[1][1]


def test_a_malformed_event_fails_without_retrying(calls):
    with pytest.raises(ValidationError):
        worker.notify_status_change.run({"sr_id": "1"})


def test_upstream_failures_are_retried_with_backoff():
    task = worker.notify_status_change
    assert task.autoretry_for == (UpstreamError,)
    assert task.max_retries == 5 and task.retry_backoff


def test_reconcile_runs_every_interval():
    entry = worker.app.conf.beat_schedule["reconcile"]
    assert entry["task"] == "querydesk.reconcile"
    assert entry["schedule"] == worker.settings.reconcile_interval_s


# --- WhatsApp ---------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(worker.whatsapp_send, "delay", lambda to, body: out.append((to, body)))
    monkeypatch.setattr(worker, "_loop", None)
    return out


def test_a_whatsapp_message_is_answered_through_the_send_task(sent, monkeypatch):
    async def reply_to(phone, body, has_media):
        return f"echo: {body}"

    monkeypatch.setattr(worker.inbound, "reply_to", reply_to)

    worker.whatsapp_message.run("+1555", "hi", False)

    assert sent == [("+1555", "echo: hi")]


def test_nothing_to_answer_sends_nothing(sent, monkeypatch):
    async def reply_to(phone, body, has_media):
        return None

    monkeypatch.setattr(worker.inbound, "reply_to", reply_to)

    worker.whatsapp_message.run("+1555", "", False)

    assert sent == []


def test_the_message_task_never_retries():
    # Running the pipeline twice could advance a conversation twice.
    assert not getattr(worker.whatsapp_message, "autoretry_for", ())


def test_a_rejected_send_is_dropped_not_retried(monkeypatch):
    async def send(to, body):
        raise WhatsAppError("invalid number", retryable=False)

    monkeypatch.setattr(worker.whatsapp, "send", send)
    monkeypatch.setattr(worker, "_loop", None)

    assert worker.whatsapp_send.run("+1555", "hi") is None


def test_a_failed_send_is_retried(monkeypatch):
    async def send(to, body):
        raise WhatsAppError("provider 503")

    monkeypatch.setattr(worker.whatsapp, "send", send)
    monkeypatch.setattr(worker, "_loop", None)
    retried = []

    def retry(exc, countdown):
        retried.append((exc, countdown))
        return Retry()

    monkeypatch.setattr(worker.whatsapp_send, "retry", retry)

    with pytest.raises(Retry):
        worker.whatsapp_send.run("+1555", "hi")
    assert retried and retried[0][1] >= 1
