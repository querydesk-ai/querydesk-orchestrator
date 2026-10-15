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


def test_next_key_is_deterministic_and_depends_on_the_closed_sr():
    assert tk.next_key("k", "1001") == tk.next_key("k", "1001")
    assert tk.next_key("k", "1001") != tk.next_key("k", "1002")
    assert tk.next_key("k", "1001") != "k"


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
async def test_a_replayed_closed_sr_chains_to_a_new_key():
    closed = {**SR_BODY, "status": "RESOLVED"}
    created = {**SR_BODY, "id": 1002}
    route = respx.post(f"{BASE}/sr").mock(
        side_effect=[httpx.Response(200, json=closed), httpx.Response(201, json=created)]
    )

    record = await tk.create_sr(SR, "key-1")

    keys = [c.request.headers["Idempotency-Key"] for c in route.calls]
    assert keys == ["key-1", tk.next_key("key-1", "1001")]
    assert record.id == "1002" and not record.replayed


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize("status", ["PENDING", "APPROVED", "IN_PROGRESS"])
async def test_a_replayed_open_sr_is_returned_as_is(status):
    route = respx.post(f"{BASE}/sr").mock(
        return_value=httpx.Response(200, json={**SR_BODY, "status": status})
    )

    record = await tk.create_sr(SR, "key-1")

    assert record.replayed and record.id == "1001"
    assert route.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_chaining_gives_up_after_max_reopens():
    respx.post(f"{BASE}/sr").mock(
        return_value=httpx.Response(200, json={**SR_BODY, "status": "REJECTED"})
    )

    with pytest.raises(tk.TicketingError, match="Gave up"):
        await tk.create_sr(SR, "key-1")
    assert respx.calls.call_count == tk.MAX_REOPENS + 1


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


@pytest.mark.anyio
@respx.mock
async def test_list_open_srs_filters_closed_ones():
    route = respx.get(f"{BASE}/sr").mock(
        return_value=httpx.Response(
            200,
            json=[
                SR_BODY,
                {**SR_BODY, "id": 1002, "status": "RESOLVED"},
                {**SR_BODY, "id": 1003, "status": "PENDING_APPROVAL"},
            ],
        )
    )
    open_srs = await tk.list_open_srs("u1")

    assert [r.id for r in open_srs] == ["1001", "1003"]
    assert route.calls.last.request.url.params["user_id"] == "u1"


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json=[{"id": 1}]),  # missing fields
        httpx.Response(200, text="not json"),
    ],
)
async def test_list_open_srs_failures_raise_ticketing_error(response):
    respx.get(f"{BASE}/sr").mock(return_value=response)
    with pytest.raises(tk.TicketingError):
        await tk.list_open_srs("u1")


@pytest.mark.anyio
@respx.mock
async def test_list_open_srs_network_error_raises_ticketing_error():
    respx.get(f"{BASE}/sr").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(tk.TicketingError):
        await tk.list_open_srs("u1")


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
async def test_stand_in_reopens_after_resolve_and_enforces_transitions(fake_service):
    first = await tk.create_sr(SR, "same-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_service), base_url=BASE
    ) as c:
        assert (
            await c.patch(f"/sr/{first.id}/status", json={"status": "RESOLVED"})
        ).status_code == 409
        for status in ("IN_PROGRESS", "RESOLVED"):
            assert (
                await c.patch(f"/sr/{first.id}/status", json={"status": status})
            ).status_code == 200

    second = await tk.create_sr(SR, "same-key")
    again = await tk.create_sr(SR, "same-key")

    assert second.id != first.id and not second.replayed
    assert again.id == second.id and again.replayed


@pytest.mark.anyio
async def test_stand_in_rejects_an_unknown_category(fake_service):
    bad = tk.SRCreate(user_id="u1", category_id="nope", description="d", requires_approval=False)

    with pytest.raises(tk.TicketingError, match="400"):
        await tk.create_sr(bad, "k")


@pytest.mark.anyio
async def test_the_http_client_is_shared_across_calls(monkeypatch):
    app = create_app()
    built = []

    def make():
        built.append(1)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)

    monkeypatch.setattr(tk, "_make_client", make)

    first = await tk.create_sr(SR, "k1")
    await tk.create_sr(SR, "k2")
    await tk.get_sr(first.id)
    assert len(built) == 1

    await tk.aclose()
    await tk.get_sr(first.id)  # rebuilt after a close
    assert len(built) == 2


@pytest.mark.anyio
async def test_stand_in_lists_only_this_users_open_srs(fake_service):
    first = await tk.create_sr(SR, "k1")
    await tk.create_sr(tk.SRCreate(**{**SR.__dict__, "user_id": "u2"}), "k2")

    assert [r.id for r in await tk.list_open_srs("u1")] == [first.id]
