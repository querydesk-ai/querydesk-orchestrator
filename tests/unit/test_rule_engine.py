"""
Rule engine: the PRD's six rules, first match wins. Offline — the extractor is
a stub and the router result is built by hand.
"""

import pytest

from querydesk.core.config import settings
from querydesk.models.schemas import Decision, ResolutionType
from querydesk.services.decision.engine import DecisionInput, RuleEngine, get_engine
from querydesk.services.decision.extraction import Extraction
from querydesk.services.decision.rules import (
    DecisionRules,
    DecisionRulesError,
    load_rules,
    parse_rules,
)
from querydesk.services.decision.signals import Intent
from querydesk.services.retrieval.qdrant_store import CategoryHit
from querydesk.services.routing.router import RouterResult

HIGH, MID = 0.56, 0.49


@pytest.fixture(autouse=True)
def thresholds(monkeypatch):
    monkeypatch.setattr(settings, "confidence_high", HIGH)
    monkeypatch.setattr(settings, "confidence_mid", MID)


class StubExtractor:
    """Returns the given extraction and records every call."""

    def __init__(self, fields=None, missing=None, ok=True):
        self.result = Extraction(fields=fields or {}, missing=missing or [], ok=ok)
        self.calls = []

    async def __call__(self, query, category):
        self.calls.append((query, category.id))
        return self.result


def _router(category_id="it_application_restart", confident=True, score=0.7):
    hit = CategoryHit(category_id=category_id, name="", description="", score=score)
    return RouterResult(
        top_two=[hit], category_id=category_id if confident else None, confident=confident
    )


def _input(
    *, score=0.7, has_hits=True, action=False, status=False, router=None, query="q"
) -> DecisionInput:
    return DecisionInput(
        query=query,
        top_dense_score=score,
        has_hits=has_hits,
        router=router or _router(),
        intent=Intent(is_action=action, is_status=status),
    )


def _engine(extractor=None, rules=None):
    return RuleEngine(extractor=extractor or StubExtractor(), rules=rules or DecisionRules())


# --- Rules 1–5 ---------------------------------------------------------------


@pytest.mark.anyio
async def test_rule1_status_beats_everything():
    """Even an action with a perfect score and a confident router."""
    ex = StubExtractor()
    out = await _engine(ex).decide(_input(status=True, action=True, score=0.99))

    assert out.decision is Decision.STATUS_LOOKUP and out.rule == 1
    assert ex.calls == []


@pytest.mark.anyio
async def test_rule2_confident_question_is_answered():
    out = await _engine().decide(_input(score=HIGH))

    assert out.decision is Decision.ANSWER
    assert out.resolution_type is ResolutionType.RAG
    assert out.rule == 2


@pytest.mark.anyio
async def test_rule3_mid_band_question_gets_a_disclaimer():
    out = await _engine().decide(_input(score=MID))

    assert out.decision is Decision.ANSWER_WITH_DISCLAIMER and out.rule == 3


@pytest.mark.anyio
async def test_an_action_never_reaches_rule2_however_well_it_matches():
    """The PRD's most important failure: answering an action request with an article."""
    ex = StubExtractor(fields={"application": "claims", "environment": "UAT"})
    out = await _engine(ex).decide(_input(action=True, score=0.95))

    assert out.decision is Decision.RAISE_SR
    assert out.resolution_type is ResolutionType.AUTO_SR
    assert out.rule == 4


@pytest.mark.anyio
async def test_rule4_complete_fields_raise_an_sr():
    ex = StubExtractor(fields={"application": "claims", "environment": "UAT"})
    out = await _engine(ex).decide(_input(action=True, query="restart claims in UAT"))

    assert out.decision is Decision.RAISE_SR
    assert out.category_id == "it_application_restart"
    assert out.fields == {"application": "claims", "environment": "UAT"}
    assert ex.calls == [("restart claims in UAT", "it_application_restart")]
    assert out.extraction_ms is not None


@pytest.mark.anyio
async def test_rule4_missing_fields_collect_details():
    ex = StubExtractor(missing=["application", "environment"])
    out = await _engine(ex).decide(_input(action=True))

    assert out.decision is Decision.COLLECT_DETAILS
    assert out.resolution_type is ResolutionType.AUTO_SR
    assert out.missing_fields == ["application", "environment"]


@pytest.mark.anyio
@pytest.mark.parametrize("has_hits", [True, False])
async def test_low_score_question_with_confident_router_escalates_not_an_sr(has_hits):
    """Narrower than the PRD's 'or a score below mid': a question never opens an SR."""
    ex = StubExtractor(fields={"application": "claims", "environment": "UAT"})
    out = await _engine(ex).decide(_input(action=False, score=MID - 0.01, has_hits=has_hits))

    assert out.decision is Decision.ESCALATE and out.rule == 5
    assert ex.calls == []


@pytest.mark.anyio
async def test_question_without_hits_skips_the_answer_rules():
    """No hits means nothing to ground an answer on, whatever the score says."""
    ex = StubExtractor(missing=["application"])
    out = await _engine(ex).decide(_input(score=0.9, has_hits=False))

    assert out.decision is Decision.ESCALATE


@pytest.mark.anyio
@pytest.mark.parametrize("action", [True, False])
async def test_rule5_unconfident_router_escalates(action):
    ex = StubExtractor()
    out = await _engine(ex).decide(
        _input(action=action, score=0.2, router=_router(confident=False))
    )

    assert out.decision is Decision.ESCALATE
    assert out.resolution_type is ResolutionType.MANUAL_ESCALATION
    assert out.rule == 5
    assert ex.calls == []  # no LLM spend on a message nobody can route


@pytest.mark.anyio
async def test_extractor_not_called_on_answer_paths():
    ex = StubExtractor()
    await _engine(ex).decide(_input(score=HIGH))
    await _engine(ex).decide(_input(score=MID))

    assert ex.calls == []


# --- Approval ----------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("category", "fields", "expected"),
    [
        ("it_uat_extension", {"environment": "UAT-2", "extend_until": "11pm", "reason": "x"}, True),
        ("it_password_reset", {}, False),  # approval: none
        ("it_application_restart", {"application": "a", "environment": "UAT"}, False),
    ],
)
async def test_approval_comes_from_the_catalog(category, fields, expected):
    out = await _engine(StubExtractor(fields=fields)).decide(
        _input(action=True, router=_router(category))
    )

    assert out.requires_approval is expected


# --- Rule 6: tighten only ----------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("env", ["prod", "PROD", "production", "prod env", "Live", "prod-eu"])
async def test_rule6_production_restart_needs_approval(env):
    ex = StubExtractor(fields={"application": "agent-portal", "environment": env})
    out = await _engine(ex, load_rules()).decide(_input(action=True))

    assert out.decision is Decision.RAISE_SR
    assert out.requires_approval is True
    assert out.overrides and "Production restarts" in out.overrides[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "env",
    [
        "UAT",
        "pre-prod staging",
        "preprod",
        "Pre-Production",
        "productivity-uat",
        "non-prod",
        "Non Production",
        "nonprod",
        "not prod",
        "prod-like UAT",
        "production-like",
    ],
)
async def test_rule6_non_production_restart_stays_unapproved(env):
    """Whole-word matching: 'productivity' must not read as 'prod'."""
    ex = StubExtractor(fields={"application": "a", "environment": env})
    out = await _engine(ex, load_rules()).decide(_input(action=True))

    assert out.requires_approval is False


@pytest.mark.anyio
async def test_rule6_commission_dispute_always_needs_approval():
    ex = StubExtractor(fields={"agent_code": "AG1", "month": "Aug"})
    out = await _engine(ex, load_rules()).decide(
        _input(action=True, router=_router("ops_commission_payout_query"))
    )

    assert out.requires_approval is True


@pytest.mark.anyio
async def test_rule6_cannot_switch_approval_off():
    """An 'always' category stays approved even when no rule matches it."""
    ex = StubExtractor(fields={"cluster": "c", "environment": "UAT", "reason": "r"})
    out = await _engine(ex, load_rules()).decide(
        _input(action=True, router=_router("it_add_server_cluster"))
    )

    assert out.requires_approval is True
    assert out.overrides == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fields", [{"application": "a", "environment": "UAT"}, {}]
)  # RAISE_SR and COLLECT_DETAILS
async def test_rule6_force_escalation_overrides_the_sr_path(fields):
    rules = parse_rules({"force_escalation": ["it_application_restart"]})
    ex = StubExtractor(fields=fields, missing=[] if fields else ["application"])
    out = await _engine(ex, rules).decide(_input(action=True))

    assert out.decision is Decision.ESCALATE
    assert out.resolution_type is ResolutionType.MANUAL_ESCALATION
    assert out.overrides == ["force_escalation: it_application_restart"]


@pytest.mark.anyio
@pytest.mark.parametrize("score", [HIGH, MID])  # rules 2 and 3
async def test_rule6_force_escalation_leaves_answers_alone(score):
    """Forcing a category's SRs to a person must not stop its articles answering questions."""
    rules = parse_rules({"force_escalation": ["it_application_restart"]})
    out = await _engine(rules=rules).decide(_input(score=score))

    assert out.decision in (Decision.ANSWER, Decision.ANSWER_WITH_DISCLAIMER)
    assert out.overrides == []


@pytest.mark.anyio
async def test_rule6_never_turns_an_escalation_into_anything_else():
    rules = parse_rules({"force_escalation": ["it_application_restart"]})
    out = await _engine(rules=rules).decide(_input(router=_router(confident=False), score=0.1))

    assert out.decision is Decision.ESCALATE and out.overrides == []


# --- Rules file validation ---------------------------------------------------


def test_shipped_rules_file_is_valid():
    rules = load_rules()
    assert rules.force_approval


@pytest.mark.parametrize(
    "raw",
    [
        {"force_approval": [{"category_id": "nope"}]},
        {"force_approval": [{"category_id": "it_application_restart", "field": "colour"}]},
        {"force_approval": [{"category_id": "it_application_restart", "field": "environment"}]},
        {"force_escalation": ["nope"]},
    ],
)
def test_bad_rules_fail_at_load(raw):
    with pytest.raises(DecisionRulesError):
        parse_rules(raw)


def test_get_engine_defaults_to_rules():
    assert get_engine().name == "rules"


def test_unknown_engine_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "decision_engine", "magic")
    with pytest.raises(ValueError):
        get_engine()
