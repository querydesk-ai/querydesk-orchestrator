"""
Semantic router tests. Category search is monkeypatched and the tie-break LLM
is stubbed, so these run offline.
"""

import pytest

from querydesk.core.config import settings
from querydesk.services.llm.fireworks import FireworksError
from querydesk.services.retrieval.qdrant_store import CategoryHit
from querydesk.services.routing import router


def _hit(category_id, score):
    return CategoryHit(
        category_id=category_id, name=category_id, description=f"{category_id} desc", score=score
    )


@pytest.fixture(autouse=True)
def thresholds(monkeypatch):
    monkeypatch.setattr(settings, "router_min_score", 0.40)
    monkeypatch.setattr(settings, "router_tie_margin", 0.03)
    monkeypatch.setattr(settings, "router_tie_break_enabled", True)


def _stub_categories(monkeypatch, hits):
    async def fake_search(dense_vector, limit=2):
        return hits

    monkeypatch.setattr(router.qdrant_store, "search_categories", fake_search)


@pytest.fixture
def llm(monkeypatch):
    """Records tie-break calls; set `reply` (str or exception) per test."""
    state = {"reply": "", "calls": []}

    async def fake_chat(messages, **kwargs):
        state["calls"].append(messages)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    monkeypatch.setattr(router, "chat_completion", fake_chat)
    return state


@pytest.mark.anyio
async def test_clear_winner_is_confident_without_an_llm_call(monkeypatch, llm):
    _stub_categories(
        monkeypatch, [_hit("it_uat_extension", 0.71), _hit("it_application_restart", 0.52)]
    )

    result = await router.route("keep uat up till 11", [0.1])

    assert result.confident
    assert result.category_id == "it_uat_extension"
    assert not result.tie_broken
    assert llm["calls"] == []


@pytest.mark.anyio
async def test_below_min_score_is_not_confident(monkeypatch, llm):
    """Outside the 10 categories: the router must not pick the least-bad one."""
    _stub_categories(
        monkeypatch, [_hit("ops_crm_lead_access", 0.21), _hit("it_password_reset", 0.19)]
    )

    result = await router.route("what is the capital of Peru", [0.1])

    assert not result.confident
    assert result.category_id is None
    assert len(result.top_two) == 2  # still reported, for decision_logs
    assert llm["calls"] == []


@pytest.mark.anyio
async def test_no_hits_is_not_confident(monkeypatch, llm):
    _stub_categories(monkeypatch, [])

    result = await router.route("q", [0.1])

    assert not result.confident
    assert result.margin is None


@pytest.mark.anyio
async def test_close_call_is_settled_by_the_llm(monkeypatch, llm):
    _stub_categories(
        monkeypatch, [_hit("it_application_restart", 0.60), _hit("it_add_server_cluster", 0.59)]
    )
    llm["reply"] = "it_add_server_cluster"

    result = await router.route("add a node to the claims cluster", [0.1])

    assert result.confident
    assert result.tie_broken
    # The LLM may overturn the similarity ranking...
    assert result.category_id == "it_add_server_cluster"
    # ...but top_two keeps the raw ranking for the logs.
    assert result.top_two[0].category_id == "it_application_restart"


@pytest.mark.anyio
async def test_tie_break_prompt_lists_both_candidates(monkeypatch, llm):
    _stub_categories(monkeypatch, [_hit("a_cat", 0.60), _hit("b_cat", 0.59)])
    llm["reply"] = "a_cat"

    await router.route("the message", [0.1])

    prompt = llm["calls"][0][0]["content"]
    assert "the message" in prompt
    assert "a_cat: a_cat desc" in prompt
    assert "b_cat: b_cat desc" in prompt


@pytest.mark.anyio
async def test_tie_break_reply_is_normalised(monkeypatch, llm):
    _stub_categories(monkeypatch, [_hit("a_cat", 0.60), _hit("b_cat", 0.59)])
    llm["reply"] = "  `B_CAT`.\n"

    result = await router.route("q", [0.1])

    assert result.category_id == "b_cat"


@pytest.mark.anyio
@pytest.mark.parametrize("reply", ["none", "c_cat", "I think it is a_cat because..."])
async def test_unusable_tie_break_reply_escalates_rather_than_guessing(monkeypatch, llm, reply):
    _stub_categories(monkeypatch, [_hit("a_cat", 0.60), _hit("b_cat", 0.59)])
    llm["reply"] = reply

    result = await router.route("q", [0.1])

    assert not result.confident
    assert result.category_id is None


@pytest.mark.anyio
async def test_fireworks_failure_in_tie_break_escalates_instead_of_502(monkeypatch, llm):
    _stub_categories(monkeypatch, [_hit("a_cat", 0.60), _hit("b_cat", 0.59)])
    llm["reply"] = FireworksError("down")

    result = await router.route("q", [0.1])

    assert not result.confident


@pytest.mark.anyio
async def test_disabled_tie_break_leaves_close_calls_unconfident(monkeypatch, llm):
    monkeypatch.setattr(settings, "router_tie_break_enabled", False)
    _stub_categories(monkeypatch, [_hit("a_cat", 0.60), _hit("b_cat", 0.59)])

    result = await router.route("q", [0.1])

    assert not result.confident
    assert llm["calls"] == []
