"""
Celery tasks, run directly (no broker): they validate the event and run the
async services on one long-lived loop per worker process.
"""

import asyncio

import pytest
from pydantic import ValidationError

from querydesk import worker
from querydesk.core.errors import UpstreamError


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
