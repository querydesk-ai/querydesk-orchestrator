"""
The decision layer: one decision per message (PRD F2).

A `DecisionEngine` is an interface so TypeSafe Jev can replace the rule engine
on Day 5 without touching the pipeline. `RuleEngine` implements the PRD's six
rules, first match wins:

  1. asks about an existing request            -> STATUS_LOOKUP
  2. not an action, has hits, score >= high    -> ANSWER                 (RAG)
  3. not an action, has hits, score >= mid     -> ANSWER_WITH_DISCLAIMER (RAG)
  4. action, router confident                  -> RAISE_SR / COLLECT_DETAILS (AUTO_SR)
  5. anything else                             -> ESCALATE               (MANUAL_ESCALATION)

Rule 4 is narrower than the PRD's wording ("an action, or a score below
mid"): a question we can't answer goes to a person, never to an SR the user
didn't ask for (docs/plan-days-4-5.md).
  6. config rules, applied last, tighten only  -> force approval / force escalation

Scores are the top *dense* similarity, never a fused RRF score (invariant #1).
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from querydesk.core.config import settings
from querydesk.models.schemas import Decision, ResolutionType
from querydesk.services import catalog
from querydesk.services.catalog import Approval, Category
from querydesk.services.decision.extraction import Extraction, extract_fields
from querydesk.services.decision.rules import DecisionRules, load_rules
from querydesk.services.decision.signals import Intent
from querydesk.services.routing.router import RouterResult

Extractor = Callable[[str, Category], Awaitable[Extraction]]


@dataclass
class DecisionInput:
    query: str
    top_dense_score: float
    has_hits: bool
    router: RouterResult
    intent: Intent


@dataclass
class DecisionOutput:
    decision: Decision
    resolution_type: ResolutionType
    rule: int
    engine: str
    category_id: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    requires_approval: bool | None = None
    # Why approval was switched on, or an escalation forced (rule 6).
    overrides: list[str] = field(default_factory=list)
    extraction_ok: bool | None = None
    extraction_ms: float | None = None


class DecisionEngine(Protocol):
    name: str

    async def decide(self, inp: DecisionInput) -> DecisionOutput: ...

    async def complete_sr(self, category_id: str, fields: dict[str, str]) -> DecisionOutput:
        """
        The SR-path decision for fields collected over several turns: RAISE_SR
        when complete, else COLLECT_DETAILS, with rule 6 applied to the merged
        fields (a prod environment given in a follow-up still needs approval).
        """
        ...


_SR_PATH = {Decision.RAISE_SR, Decision.COLLECT_DETAILS}


class RuleEngine:
    name = "rules"

    def __init__(self, extractor: Extractor = extract_fields, rules: DecisionRules | None = None):
        self._extract = extractor
        self._rules = rules if rules is not None else load_rules()

    async def decide(self, inp: DecisionInput) -> DecisionOutput:
        out = await self._first_match(inp)
        return self._tighten(out)

    async def _first_match(self, inp: DecisionInput) -> DecisionOutput:
        router_category = inp.router.category_id if inp.router.confident else None

        # 1. A question about an existing request beats everything else.
        if inp.intent.is_status:
            return DecisionOutput(
                Decision.STATUS_LOOKUP, ResolutionType.MANUAL_ESCALATION, 1, self.name
            )

        if not inp.intent.is_action and inp.has_hits:
            # 2. Confident enough to answer outright.
            if inp.top_dense_score >= settings.confidence_high:
                return DecisionOutput(
                    Decision.ANSWER, ResolutionType.RAG, 2, self.name, router_category
                )
            # 3. Probably covered: answer, but say so and offer an SR.
            if inp.top_dense_score >= settings.confidence_mid:
                return DecisionOutput(
                    Decision.ANSWER_WITH_DISCLAIMER,
                    ResolutionType.RAG,
                    3,
                    self.name,
                    router_category,
                )

        # 4. Heading for an SR — only for an action, and only into a category
        # we're sure of. A question that fell through rules 2-3 escalates.
        if inp.intent.is_action and router_category is not None:
            return await self._sr_path(inp.query, router_category)

        # 5. Not answerable, not routable: a person takes it.
        return DecisionOutput(Decision.ESCALATE, ResolutionType.MANUAL_ESCALATION, 5, self.name)

    async def complete_sr(self, category_id: str, fields: dict[str, str]) -> DecisionOutput:
        required = catalog.get(category_id).required_fields
        extraction = Extraction(
            fields=dict(fields), missing=[name for name in required if name not in fields]
        )
        return self._tighten(self._sr_output(category_id, extraction))

    async def _sr_path(self, query: str, category_id: str) -> DecisionOutput:
        category = catalog.get(category_id)
        start = time.perf_counter()
        extraction = await self._extract(query, category)
        extraction_ms = round((time.perf_counter() - start) * 1000, 1)
        return self._sr_output(category_id, extraction, extraction_ms)

    def _sr_output(
        self, category_id: str, extraction: Extraction, extraction_ms: float | None = None
    ) -> DecisionOutput:
        category = catalog.get(category_id)
        return DecisionOutput(
            decision=Decision.RAISE_SR if extraction.complete else Decision.COLLECT_DETAILS,
            resolution_type=ResolutionType.AUTO_SR,
            rule=4,
            engine=self.name,
            category_id=category_id,
            fields=extraction.fields,
            missing_fields=extraction.missing,
            requires_approval=category.approval is Approval.ALWAYS,
            extraction_ok=extraction.ok,
            extraction_ms=extraction_ms,
        )

    def _tighten(self, out: DecisionOutput) -> DecisionOutput:
        """
        Rule 6. Only ever adds approval or forces an escalation — and only on
        the SR path: forcing a category to a person means its *requests* need
        one, not that its KB articles stop answering questions.
        """
        if out.decision in _SR_PATH and out.category_id in self._rules.force_escalation:
            out.overrides.append(f"force_escalation: {out.category_id}")
            out.decision = Decision.ESCALATE
            out.resolution_type = ResolutionType.MANUAL_ESCALATION
            return out

        if out.decision in _SR_PATH:
            notes = self._rules.approval_notes(out.category_id, out.fields)
            if notes and not out.requires_approval:
                out.requires_approval = True
                out.overrides.extend(notes)
        return out


def get_engine() -> DecisionEngine:
    if settings.decision_engine == "rules":
        return RuleEngine()
    raise ValueError(f"Unknown decision_engine {settings.decision_engine!r} (expected 'rules')")
