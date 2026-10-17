"""
Status answers against the in-process stand-in ticketing service: a user's
open SRs, one SR by number, and nothing about anyone else's.
"""

import httpx
import pytest

from querydesk.core.config import settings
from querydesk.services import catalog, status
from querydesk.services.ticketing import client as tk
from tests.fakes.fake_ticketing import create_app

BASE = "http://ticketing.test"
UAT = "it_uat_extension"
RESTART = "it_application_restart"


@pytest.fixture
def service(monkeypatch):
    app = create_app()
    monkeypatch.setattr(settings, "ticketing_base_url", BASE)
    monkeypatch.setattr(
        tk,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE),
    )
    return app


async def _raise(category_id, user_id="u1", key=None, approval=False):
    sr = tk.SRCreate(
        user_id=user_id, category_id=category_id, description="d", requires_approval=approval
    )
    return await tk.create_sr(sr, key or f"{user_id}-{category_id}")


async def _move(app, sr_id, *statuses):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        for s in statuses:
            assert (await c.patch(f"/sr/{sr_id}/status", json={"status": s})).status_code == 200


@pytest.mark.anyio
async def test_no_open_requests(service):
    assert await status.answer("u1", "status of my requests") == "You have no open requests."


@pytest.mark.anyio
async def test_open_srs_are_listed_newest_first_with_names_and_labels(service):
    uat = await _raise(UAT, approval=True)
    restart = await _raise(RESTART)
    await _move(service, uat.id, "APPROVED")  # now the most recently updated

    reply = await status.answer("u1", "status of my requests")

    lines = reply.splitlines()
    assert lines[0] == "You have 2 open requests:"
    assert lines[1].startswith(f"- SR {uat.id}: {catalog.get(UAT).name}, Approved (updated ")
    assert lines[2].startswith(f"- SR {restart.id}: {catalog.get(RESTART).name}, Pending")


@pytest.mark.anyio
async def test_closed_and_other_users_srs_are_not_listed(service):
    done = await _raise(RESTART)
    await _move(service, done.id, "IN_PROGRESS", "RESOLVED")
    await _raise(UAT, user_id="u2")
    mine = await _raise(UAT)

    reply = await status.answer("u1", "my open requests")

    assert reply.splitlines()[0] == "You have 1 open request:"
    assert f"SR {mine.id}" in reply and f"SR {done.id}" not in reply


@pytest.mark.anyio
async def test_a_long_list_is_capped(service):
    for i in range(status.MAX_LISTED + 2):
        await _raise(RESTART, key=f"k{i}")

    lines = (await status.answer("u1", "status of my requests")).splitlines()

    assert len(lines) == 1 + status.MAX_LISTED + 1
    assert lines[-1] == "…and 2 more."


@pytest.mark.anyio
async def test_one_sr_by_number_including_a_closed_one(service):
    sr = await _raise(RESTART)
    await _move(service, sr.id, "IN_PROGRESS", "RESOLVED")

    reply = await status.answer("u1", f"any update on SR-{sr.id}?")

    assert reply.startswith(f"SR {sr.id}: {catalog.get(RESTART).name}, Resolved")


@pytest.mark.anyio
async def test_someone_elses_sr_reads_exactly_like_a_missing_one(service):
    theirs = await _raise(UAT, user_id="u2")

    other = await status.answer("u1", f"status of SR {theirs.id}")
    missing = await status.answer("u1", "status of SR 999999")

    assert other == f"I can't find SR {theirs.id} among your requests."
    assert missing == "I can't find SR 999999 among your requests."
