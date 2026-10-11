"""Report analysis on a tiny synthetic run — checks the wiring, not real numbers."""

from querydesk.eval.report import analyse, render_markdown


def _row(id, kind, category, dense, top_two, sources=(), expected_source=None, llm=None, **kw):
    return {
        "id": id,
        "query": f"query {id}",
        "kind": kind,
        "category_id": category,
        "expected_source": expected_source,
        "lexical": kw.get("lexical", False),
        "top_dense_score": dense,
        "sources": list(sources),
        "top_two": top_two,
        "llm_choice": llm,
        "latency_ms": {"embed": 100.0, "search": 20.0, "route": 10.0, "tie_break": None},
    }


RUN = [
    # Clear win.
    _row("a1", "action", "cat_a", 0.70, [["cat_a", 0.70], ["cat_b", 0.40]]),
    # Close call, similarity wrong, LLM right.
    _row("a2", "action", "cat_b", 0.65, [["cat_a", 0.55], ["cat_b", 0.54]], llm="cat_b"),
    _row(
        "i1",
        "informational",
        "cat_a",
        0.60,
        [["cat_a", 0.60], ["cat_b", 0.30]],
        sources=["a.md", "b.md"],
        expected_source="a.md",
        lexical=True,
    ),
    _row(
        "i2",
        "informational",
        "cat_b",
        0.50,
        [["cat_b", 0.50], ["cat_a", 0.20]],
        sources=["a.md", "b.md"],
        expected_source="b.md",
    ),
    _row("o1", "out_of_scope", None, 0.20, [["cat_a", 0.25], ["cat_b", 0.24]]),
]


CURRENT = {
    "confidence_high": 0.75,
    "confidence_mid": 0.55,
    "router_min_score": 0.40,
    "router_tie_margin": 0.03,
}


def test_tie_break_rescues_the_close_call():
    a = analyse(RUN)
    sr = a.category["sr_bound"]
    assert sr["top1"] == 0.5  # a2 is wrong on similarity alone
    assert a.proposals["router_tie_margin"] > 0.01  # a2's margin falls below it
    assert sr["with_tie_break"] == 1.0


def test_tie_break_is_not_measured_when_the_run_skipped_it():
    """A run without --tie-break must not count every close call as a miss."""
    skipped = [{**r, "llm_choice": None, "tie_break_ran": False} for r in RUN]
    a = analyse(skipped)

    assert a.category["sr_bound"]["with_tie_break"] is None
    assert a.category["sr_bound"]["top1"] == 0.5
    assert "| n/a |" in render_markdown(a, "run.jsonl", CURRENT)


def test_retrieval_hits_and_lexical_rows():
    a = analyse(RUN)
    assert a.retrieval["hit_at_1"] == 0.5  # i2 cites b.md second
    assert a.retrieval["hit_at_k"] == 1.0
    assert a.lexical_rows == [{"id": "i1", "query": "query i1", "hit_at_1": True, "hit_at_k": True}]


def test_thresholds_separate_informational_from_out_of_scope():
    a = analyse(RUN)
    p = a.proposals
    assert p["confidence_high"] >= p["confidence_mid"]
    assert p["confidence_high"] > 0.20  # above the out-of-scope KB score
    assert p["router_min_score"] > 0.25 or a.effects["oos_routed"] <= 0.10


def test_misrouted_lists_only_wrong_top1():
    assert [m["id"] for m in analyse(RUN).misrouted] == ["a2"]


def test_markdown_renders_every_section():
    md = render_markdown(analyse(RUN), "run.jsonl", CURRENT)
    for heading in ("Proposed thresholds", "Category accuracy", "Day 2", "Latency", "Misrouted"):
        assert heading in md


def _decided(row, decision, rule=4, missing=()):
    return {**row, "decision": decision, "rule": rule, "missing_fields": list(missing)}


def _labelled(id, kind, expected):
    return {
        **_row(id, kind, None if kind == "out_of_scope" else "cat_a", 0.6, [["cat_a", 0.6]]),
        "expected_decision": expected,
    }


DECIDED = [
    _decided(_labelled("a1", "action", "RAISE_SR"), "RAISE_SR"),
    _decided(_labelled("a2", "action", "COLLECT_DETAILS"), "ANSWER", rule=2),  # the bad one
    _decided(_labelled("i1", "informational", "ANSWER"), "ANSWER_WITH_DISCLAIMER", rule=3),
    _decided(_labelled("i2", "informational", "ANSWER"), "ANSWER", rule=2),
    _decided(_labelled("o1", "out_of_scope", "ESCALATE"), "ESCALATE", rule=5),
]


def test_decision_metrics_strict_and_lenient():
    d = analyse(DECIDED).decisions
    assert d["strict"] == 3 / 5  # the disclaimer answer counts as wrong only here
    assert d["lenient"] == 4 / 5
    assert d["by_kind"] == {"action": 0.5, "informational": 1.0, "out_of_scope": 1.0}


def test_action_answered_with_an_article_is_measured():
    """The PRD's most important failure, reported on its own."""
    assert analyse(DECIDED).decisions["action_answered"] == 0.5


def test_confusion_and_wrong_rows():
    d = analyse(DECIDED).decisions
    assert d["confusion"]["COLLECT_DETAILS"] == {"ANSWER": 1}
    assert d["confusion"]["ANSWER"] == {"ANSWER_WITH_DISCLAIMER": 1, "ANSWER": 1}
    assert [w["id"] for w in d["wrong"]] == ["a2"]


def test_runs_without_decisions_skip_the_section():
    """Day 3 result files have no decision column; the report must still build."""
    assert analyse(RUN).decisions is None
    md = render_markdown(analyse(RUN), "run.jsonl", CURRENT)
    assert "Decision layer" not in md


def test_decision_section_renders():
    md = render_markdown(analyse(DECIDED), "run.jsonl", CURRENT, title="Eval report v1")
    assert md.startswith("# Eval report v1")
    assert "Routing accuracy" in md and "answered with an article" in md
    assert "| a2 |" in md


def test_zero_ms_stages_are_counted_not_dropped():
    """A 0 ms stage is a real (fast) measurement; only a missing one is skipped."""
    rows = [{**r, "latency_ms": {**r["latency_ms"], "decide": 0.0}} for r in RUN]
    assert analyse(rows).latency["decide"]["n"] == len(RUN)
    assert analyse(RUN).latency["tie_break"]["n"] == 0  # None everywhere
