"""
The eval set's shape matches the PRD, and its labels agree with
categories.json and the KB. Pure file checks — no network.
"""

import json
from collections import Counter

import pytest

from querydesk.core.config import settings
from querydesk.eval.schema import Decision, EvalRow, Kind, load_eval_set


@pytest.fixture(scope="module")
def rows():
    return load_eval_set()


@pytest.fixture(scope="module")
def category_ids():
    catalog = json.loads(settings.categories_file.read_text(encoding="utf-8"))
    return {c["id"] for c in catalog["categories"]}


def test_meets_the_prd_split_minimums(rows):
    """PRD: 30 action, 20 informational, 10 out-of-scope — at least; the set grows past it."""
    kinds = Counter(r.kind for r in rows)
    assert kinds[Kind.ACTION] >= 30
    assert kinds[Kind.INFORMATIONAL] >= 20
    assert kinds[Kind.OUT_OF_SCOPE] >= 10


def test_ids_are_unique(rows):
    assert len({r.id for r in rows}) == len(rows)


def test_every_category_has_at_least_3_action_and_2_informational(rows, category_ids):
    action = Counter(r.category_id for r in rows if r.kind is Kind.ACTION)
    info = Counter(r.category_id for r in rows if r.kind is Kind.INFORMATIONAL)
    assert set(action) == category_ids and min(action.values()) >= 3
    assert set(info) == category_ids and min(info.values()) >= 2


def test_categories_come_from_categories_json(rows, category_ids):
    """Invariant #3: no category id that categories.json doesn't define."""
    used = {r.category_id for r in rows if r.category_id}
    assert used <= category_ids


def test_both_action_outcomes_are_covered(rows):
    """Otherwise COLLECT_DETAILS accuracy can't be measured at all."""
    decisions = Counter(r.expected_decision for r in rows if r.kind is Kind.ACTION)
    assert decisions[Decision.RAISE_SR] > 0
    assert decisions[Decision.COLLECT_DETAILS] > 0


def test_lexical_rows_exist_for_the_bm25_check(rows):
    assert sum(r.lexical for r in rows) >= 2


@pytest.mark.skipif(not settings.kb_dir.is_dir(), reason="data/kb is gitignored; absent in CI")
def test_expected_sources_exist_in_the_kb(rows):
    kb_files = {p.name for p in settings.kb_dir.glob("*.md")}
    missing = {r.expected_source for r in rows if r.expected_source} - kb_files
    assert not missing


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "out_of_scope", "expected_decision": "ESCALATE", "category_id": "x"},
        {"kind": "action", "expected_decision": "RAISE_SR", "category_id": "x"},
        {
            "kind": "action",
            "expected_decision": "RAISE_SR",
            "category_id": "x",
            "fields_present": False,
        },
        {"kind": "informational", "expected_decision": "ANSWER", "category_id": "x"},
    ],
)
def test_inconsistent_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        EvalRow(id="r", query="q", **bad)
