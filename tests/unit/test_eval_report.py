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


def test_tie_break_rescues_the_close_call():
    a = analyse(RUN)
    sr = a.category["sr_bound"]
    assert sr["top1"] == 0.5  # a2 is wrong on similarity alone
    assert a.proposals["router_tie_margin"] > 0.01  # a2's margin falls below it
    assert sr["with_tie_break"] == 1.0


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
    current = {
        "confidence_high": 0.75,
        "confidence_mid": 0.55,
        "router_min_score": 0.40,
        "router_tie_margin": 0.03,
    }
    md = render_markdown(analyse(RUN), "run.jsonl", current)
    for heading in ("Proposed thresholds", "Category accuracy", "Day 2", "Latency", "Misrouted"):
        assert heading in md
