"""
Ticketing client. respx pins down the HTTP contract; a second set of tests
runs against the in-process stand-in service, so idempotency is exercised
end to end with no network.
"""

import json
from datetime import date

import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.core.errors import UpstreamError
from querydesk.services.ticketing import client as tk
from tests.fakes.fake_ticketing import create_app

BASE = "http://ticketing.test"

SR = tk.SRCreate(
    user_id="u1",
    category_id="it_application_restart",
    description="restart the claims app in UAT",
    requires_approval=False,
    metadata={"application": "claims", "environment": "UAT"},
)
SR_BODY = {
    "id": 1001,
    "status": "PENDING",
    "category_id": "it_application_restart",
    "requires_approval": False,
}


@pytest.fixture(autouse=True)
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_base_url", BASE)


# --- Idempotency key ----------------------------------------------------------


def test_key_is_deterministic_and_normalises_values():
    d = date(2026, 10, 11)
    a = tk.idempotency_key("u1", "c", {"environment": "UAT ", "app": "Claims"}, d)
    b = tk.idempotency_key("u1", "c", {"app": "claims", "environment": " uat"}, d)
    assert a == b
    assert len(a) == 32


@pytest.mark.parametrize(
    "other",
    [
        ("u2", "c", {"environment": "uat"}, date(2026, 10, 11)),  # another user
        ("u1", "d", {"environment": "uat"}, date(2026, 10, 11)),  # another category
        ("u1", "c", {"environment": "prod"}, date(2026, 10, 11)),  # another request
        ("u1", "c", {"environment": "uat"}, date(2026, 10, 12)),  # another day
    ],
)
def test_key_differs_for_a_different_request(other):
    base = tk.idempotency_key("u1", "c", {"environment": "uat"}, date(2026, 10, 11))
    assert tk.idempotency_key(*other) != base


# --- HTTP contract (respx) ----------------------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_create_sends_the_contract_payload_and_header():
    route = respx.post(f"{BASE}/sr").mock(return_value=httpx.Response(201, json=SR_BODY))

    record = await tk.create_sr(SR, "key-1")

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "key-1"
    assert json.loads(request.content) == {
        "user_id": "u1",
        "category_id": "it_application_restart",
        "description": "restart the claims app in UAT",
        "metadata": {"application": "claims", "environment": "UAT"},
        "requires_approval": False,
    }
    assert record == tk.SRRecord("1001", "PENDING", "it_application_restart", False, False)


@pytest.mark.anyio
@respx.mock
async def test_a_200_is_a_replay_of_the_original():
    respx.post(f"{BASE}/sr").mock(return_value=httpx.Response(200, json=SR_BODY))

    record = await tk.create_sr(SR, "key-1")

    assert record.replayed and record.id == "1001"


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"detail": "Unknown category"}),
        httpx.Response(409, text="conflict"),
        httpx.Response(500, text="boom"),
        httpx.Response(201, json={"unexpected": True}),  # malformed success
        httpx.Response(201, text="<html>"),
    ],
)
async def test_failures_raise_ticketing_error(response):
    respx.post(f"{BASE}/sr").mock(return_value=response)

    with pytest.raises(tk.TicketingError):
        await tk.create_sr(SR, "key-1")


@pytest.mark.anyio
@respx.mock
async def test_network_error_raises_ticketing_error():
    respx.post(f"{BASE}/sr").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(tk.TicketingError, match="unreachable"):
        await tk.create_sr(SR, "key-1")


@pytest.mark.anyio
async def test_missing_base_url_raises_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_base_url", "")

    with pytest.raises(tk.TicketingError, match="TICKETING_BASE_URL"):
        await tk.create_sr(SR, "key-1")


def test_ticketing_error_is_an_upstream_error():
    """So /chat maps it to 502 with no change to the route."""
    assert issubclass(tk.TicketingError, UpstreamError)


@pytest.mark.anyio
@respx.mock
async def test_get_sr():
    respx.get(f"{BASE}/sr/1001").mock(return_value=httpx.Response(200, json=SR_BODY))
    assert (await tk.get_sr("1001")).status == "PENDING"

    respx.get(f"{BASE}/sr/9").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(tk.TicketingError):
        await tk.get_sr("9")


# --- Against the stand-in service (in process) -------------------------------


@pytest.fixture
def fake_service(monkeypatch):
    app = create_app()
    monkeypatch.setattr(
        tk,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE),
    )
    return app


@pytest.mark.anyio
async def test_stand_in_creates_then_replays(fake_service):
    first = await tk.create_sr(SR, "same-key")
    again = await tk.create_sr(SR, "same-key")
    other = await tk.create_sr(SR, "other-key")

    assert not first.replayed and first.status == "PENDING"
    assert again.replayed and again.id == first.id
    assert other.id != first.id
    assert (await tk.get_sr(first.id)).id == first.id


@pytest.mark.anyio
async def test_stand_in_rejects_an_unknown_category(fake_service):
    bad = tk.SRCreate(user_id="u1", category_id="nope", description="d", requires_approval=False)

    with pytest.raises(tk.TicketingError, match="400"):
        await tk.create_sr(bad, "k")
