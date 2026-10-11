"""
PRD decision rule 6: config rules that apply last and can only tighten.

Loaded from data/decision_rules.json and validated against the catalog, so a
typo in a category id or field name fails at load instead of silently never
matching. Tighten-only is structural: the rules can *add* approval or *force*
an escalation, and there is no rule shape that removes either.
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from querydesk.core.config import settings
from querydesk.services import catalog

# Phrases that contain "prod" but mean *not* production. Each collapses to one
# word before matching, so the bare "prod" inside them can't match.
_NOT_PROD = (
    (re.compile(r"\bpre[\s_-]?prod(uction)?\b"), "preprod"),
    (re.compile(r"\b(non|not)[\s_-]?prod(uction)?\b"), "nonprod"),
    (re.compile(r"\bprod(uction)?[\s_-]?like\b"), "prodlike"),
)


class DecisionRulesError(ValueError):
    """decision_rules.json references something categories.json doesn't define."""


@dataclass(frozen=True)
class ForceApproval:
    category_id: str
    note: str
    # Both None: applies to every SR in the category.
    field: str | None = None
    values: tuple[str, ...] = ()

    def matches(self, category_id: str | None, fields: dict[str, str]) -> bool:
        if category_id != self.category_id:
            return False
        if self.field is None:
            return True
        value = fields.get(self.field)
        if not value:
            return False
        # Whole-word, case-insensitive: "PROD", "prod-eu" and "production"
        # match; "productivity" doesn't. "pre-prod", "non-prod" and
        # "prod-like" are collapsed to one word first so they don't read as
        # "prod" — the KB says non-production restarts need no approval.
        text = value.lower()
        for pattern, word in _NOT_PROD:
            text = pattern.sub(word, text)
        words = set(re.findall(r"[a-z0-9]+", text))
        return any(v.lower() in words for v in self.values)


@dataclass(frozen=True)
class DecisionRules:
    force_approval: tuple[ForceApproval, ...] = ()
    force_escalation: frozenset[str] = field(default_factory=frozenset)

    def approval_notes(self, category_id: str | None, fields: dict[str, str]) -> list[str]:
        return [r.note for r in self.force_approval if r.matches(category_id, fields)]


def parse_rules(raw: dict) -> DecisionRules:
    known = catalog.all_categories()
    approvals = []
    for rule in raw.get("force_approval", []):
        cid = rule["category_id"]
        if cid not in known:
            raise DecisionRulesError(f"force_approval: unknown category {cid!r}")
        name = rule.get("field")
        if name is not None and name not in known[cid].required_fields:
            raise DecisionRulesError(f"force_approval: {cid} has no required field {name!r}")
        if name is not None and not rule.get("values"):
            raise DecisionRulesError(f"force_approval: {cid}.{name} needs non-empty values")
        approvals.append(
            ForceApproval(
                category_id=cid,
                note=rule.get("note", ""),
                field=name,
                values=tuple(rule.get("values", ())),
            )
        )

    escalations = frozenset(raw.get("force_escalation", []))
    unknown = escalations - set(known)
    if unknown:
        raise DecisionRulesError(f"force_escalation: unknown categories {sorted(unknown)}")
    return DecisionRules(force_approval=tuple(approvals), force_escalation=escalations)


@lru_cache(maxsize=4)
def _load(path: Path) -> DecisionRules:
    return parse_rules(json.loads(path.read_text(encoding="utf-8")))


def load_rules() -> DecisionRules:
    return _load(settings.decision_rules_file)
