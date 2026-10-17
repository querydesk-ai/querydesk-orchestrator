"""
Status notifications: message text, the single idempotent write path, and
the reconciliation poll. Postgres is a fake that keeps the (sr_id, status)
uniqueness; the ticketing service is mocked with respx; the watermark lives
on fakeredis.
"""

from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.core.errors import PostgresStoreError
from querydesk.services import catalog
from querydesk.services.notifications import notify
from querydesk.services.state import redis_store
from querydesk.services.ticketing import client as tk

BASE = "http://ticketing.test"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
UAT = "it_uat_extension"


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.calls = []

    async def record_status_change(self, *, sr_id, user_id, status, message, lower_statuses):
        self.calls.append(
            {"sr_id": sr_id, "status": status, "message": message, "lower": lower_statuses}
        )
        if (sr_id, status) in self.rows:
            return False
        self.rows[(sr_id, status)] = (user_id, message)
        return True


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(notify.postgres_store, "record_status_change", fake.record_status_change)
    return fake


@pytest.fixture(autouse=True)
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_base_url", BASE)


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis_store, "_client", client)
    return client


# --- Messages -------------------------------------------------------------------


def test_message_names_the_sr_category_and_status():
    text = notify.message_for("1001", "APPROVED", "UAT environment extension", None)
    assert text == (
        "Your request SR 1001 (UAT environment extension) was approved. Work will start shortly."
    )


def test_message_appends_the_note_and_survives_an_unknown_status():
    text = notify.message_for("7", "ON_HOLD", None, "  waiting on vendor ")
    assert text == "Your request SR 7 moved to ON_HOLD. Note: waiting on vendor"


# --- handle_status_change -------------------------------------------------------


@pytest.mark.anyio
async def test_a_status_change_is_written_once_with_the_catalog_name(store):
    kwargs = {"sr_id": "1001", "user_id": "u1", "to_status": "APPROVED", "category_id": UAT}

    assert await notify.handle_status_change(**kwargs) is True
    assert await notify.handle_status_change(**kwargs) is False  # retried webhook

    [(user_id, message)] = store.rows.values()
    assert user_id == "u1"
    assert catalog.get(UAT).name in message
    assert store.calls[0]["lower"] == ["PENDING"]


@pytest.mark.anyio
async def test_pending_is_never_notified(store):
    assert not await notify.handle_status_change(
        sr_id="1001", user_id="u1", to_status="PENDING", category_id=UAT
    )
    assert store.calls == []


@pytest.mark.anyio
async def test_an_sr_only_moves_up_from_lower_statuses(store):
    await notify.handle_status_change(
        sr_id="1", user_id="u1", to_status="RESOLVED", category_id=UAT
    )
    assert set(store.calls[0]["lower"]) == {"PENDING", "APPROVED", "REJECTED", "IN_PROGRESS"}


@pytest.mark.anyio
@respx.mock
async def test_the_webhook_category_comes_from_the_ticketing_service(store):
    respx.get(f"{BASE}/sr/1001").mock(
        return_value=httpx.Response(
            200,
            json={"id": 1001, "status": "APPROVED", "category_id": UAT, "requires_approval": True},
        )
    )

    await notify.handle_status_change(sr_id="1001", user_id="u1", to_status="APPROVED")

    assert catalog.get(UAT).name in store.calls[0]["message"]


@pytest.mark.anyio
@respx.mock
async def test_a_failed_category_lookup_still_notifies(store):
    respx.get(f"{BASE}/sr/1001").mock(return_value=httpx.Response(503))

    assert await notify.handle_status_change(sr_id="1001", user_id="u1", to_status="APPROVED")
    assert store.calls[0]["message"].startswith("Your request SR 1001 was approved.")


@pytest.mark.anyio
@respx.mock
async def test_an_sr_the_service_does_not_know_still_notifies(store):
    respx.get(f"{BASE}/sr/1001").mock(return_value=httpx.Response(404, json={}))

    assert await notify.handle_status_change(sr_id="1001", user_id="u1", to_status="APPROVED")
    assert store.calls[0]["message"].startswith("Your request SR 1001 was approved.")


@pytest.mark.anyio
async def test_a_failed_write_raises_so_the_task_retries(monkeypatch):
    async def down(**kwargs):
        raise PostgresStoreError("down")

    monkeypatch.setattr(notify.postgres_store, "record_status_change", down)

    with pytest.raises(PostgresStoreError):
        await notify.handle_status_change(
            sr_id="1", user_id="u1", to_status="APPROVED", category_id=UAT
        )


# --- reconcile --------------------------------------------------------------------


def _sr(sr_id, status, updated_at, user_id="u1"):
    return {
        "id": sr_id,
        "status": status,
        "category_id": UAT,
        "requires_approval": True,
        "user_id": user_id,
        "updated_at": updated_at,
    }


@pytest.mark.anyio
@respx.mock
async def test_first_reconcile_looks_back_the_full_window_and_sets_the_watermark(store, fake_redis):
    route = respx.get(f"{BASE}/sr").mock(
        return_value=httpx.Response(
            200,
            json=[
                _sr(1, "APPROVED", "2026-10-16T11:00:00+00:00"),
                _sr(2, "PENDING", "2026-10-16T11:30:00+00:00"),
                _sr(3, "RESOLVED", "2026-10-16T11:10:00+00:00"),
            ],
        )
    )

    assert await notify.reconcile(now=NOW) == 2  # PENDING is silent

    since = route.calls.last.request.url.params["updated_since"]
    assert since == (NOW - timedelta(seconds=settings.reconcile_first_lookback_s)).isoformat()
    assert set(store.rows) == {("1", "APPROVED"), ("3", "RESOLVED")}
    assert await redis_store.get_json(notify.WATERMARK_KEY) == "2026-10-16T11:30:00+00:00"


@pytest.mark.anyio
@respx.mock
async def test_next_reconcile_overlaps_the_watermark_and_dedupes(store, fake_redis):
    await redis_store.set_json(notify.WATERMARK_KEY, "2026-10-16T11:30:00+00:00", 60)
    store.rows[("1", "APPROVED")] = ("u1", "already told")
    route = respx.get(f"{BASE}/sr").mock(
        return_value=httpx.Response(200, json=[_sr(1, "APPROVED", "2026-10-16T11:29:00+00:00")])
    )

    assert await notify.reconcile(now=NOW) == 0

    since = datetime.fromisoformat(route.calls.last.request.url.params["updated_since"])
    overlap = timedelta(seconds=settings.reconcile_overlap_s)
    assert since == datetime(2026, 10, 16, 11, 30, tzinfo=UTC) - overlap
    # The overlap only returned older SRs: the watermark doesn't move back.
    assert await redis_store.get_json(notify.WATERMARK_KEY) == "2026-10-16T11:30:00+00:00"


@pytest.mark.anyio
@respx.mock
async def test_a_quiet_reconcile_moves_the_watermark_to_now(store, fake_redis):
    respx.get(f"{BASE}/sr").mock(return_value=httpx.Response(200, json=[]))

    assert await notify.reconcile(now=NOW) == 0
    assert await redis_store.get_json(notify.WATERMARK_KEY) == NOW.isoformat()


@pytest.mark.anyio
@respx.mock
async def test_a_failed_poll_leaves_the_watermark(store, fake_redis):
    await redis_store.set_json(notify.WATERMARK_KEY, "2026-10-16T11:30:00+00:00", 60)
    respx.get(f"{BASE}/sr").mock(return_value=httpx.Response(500))

    with pytest.raises(tk.TicketingError):
        await notify.reconcile(now=NOW)
    assert await redis_store.get_json(notify.WATERMARK_KEY) == "2026-10-16T11:30:00+00:00"


@pytest.mark.anyio
@respx.mock
async def test_reconcile_without_redis_still_runs(store):
    # conftest leaves REDIS_URL empty: no watermark, full lookback, no error.
    respx.get(f"{BASE}/sr").mock(
        return_value=httpx.Response(200, json=[_sr(1, "APPROVED", "2026-10-16T11:00:00+00:00")])
    )

    assert await notify.reconcile(now=NOW) == 1
