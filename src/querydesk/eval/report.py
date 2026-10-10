"""
Turn an eval run (scripts/eval_run.py output) into metrics, proposed
thresholds and a markdown report. Pure: no I/O, no network.

The proposals are *proposals*. A human copies them into core/config.py, so a
bad eval run can never silently retune production (invariant #4).
"""

from dataclasses import dataclass, field
from statistics import median

from querydesk.eval import calibration as cal

# Policy knobs for the proposals, stated once so the report can print them.
OOS_ROUTED_MAX_RATE = 0.10  # at most 1 in 10 out-of-scope messages gets a category
OOS_ANSWERED_MAX_RATE = 0.0  # no out-of-scope message clears confidence_high
INFO_KEEP_RATE = 0.90  # confidence_mid keeps 90% of answerable questions
TOP1_TARGET_ACCURACY = 0.95  # above the tie margin, similarity alone must be this right


@dataclass
class Analysis:
    n_rows: int
    proposals: dict[str, float]
    category: dict[str, dict[str, float]]  # population -> metric -> value
    retrieval: dict[str, float]
    lexical_rows: list[dict]
    distributions: dict[str, dict[str, float]]
    effects: dict[str, float]
    latency: dict[str, dict[str, float]]
    misrouted: list[dict] = field(default_factory=list)


def _top1(r):
    return r["top_two"][0][0] if r["top_two"] else None


def _margin(r):
    if len(r["top_two"]) < 2:
        return 1.0
    return r["top_two"][0][1] - r["top_two"][1][1]


def _with_tie_break(r, margin: float):
    """The category the router would pick at this tie margin (None = escalate)."""
    if _margin(r) >= margin:
        return _top1(r)
    return r.get("llm_choice")


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "min": 0.0, "median": 0.0, "max": 0.0}
    return {"n": len(values), "min": min(values), "median": median(values), "max": max(values)}


def _accuracy(rows, pick) -> float:
    return sum(pick(r) == r["category_id"] for r in rows) / len(rows) if rows else 0.0


def analyse(results: list[dict]) -> Analysis:
    action = [r for r in results if r["kind"] == "action"]
    info = [r for r in results if r["kind"] == "informational"]
    oos = [r for r in results if r["kind"] == "out_of_scope"]
    in_scope = action + info

    def top1_score(r):
        return r["top_two"][0][1] if r["top_two"] else 0.0

    # --- Proposals -------------------------------------------------------
    router_min = cal.lowest_threshold_limiting([top1_score(r) for r in oos], OOS_ROUTED_MAX_RATE)
    tie_margin = cal.lowest_safe_margin(
        [(_margin(r), _top1(r) == r["category_id"]) for r in in_scope], TOP1_TARGET_ACCURACY
    )
    mid = cal.highest_threshold_keeping([r["top_dense_score"] for r in info], INFO_KEEP_RATE)
    high = max(
        mid,
        cal.lowest_threshold_limiting([r["top_dense_score"] for r in oos], OOS_ANSWERED_MAX_RATE),
    )
    proposals = {
        "confidence_high": high,
        "confidence_mid": mid,
        "router_min_score": router_min,
        "router_tie_margin": tie_margin,
    }

    # --- Category accuracy -------------------------------------------------
    category = {}
    for name, rows in (("in_scope", in_scope), ("sr_bound", action)):
        category[name] = {
            "n": len(rows),
            "top1": _accuracy(rows, _top1),
            "top2": (
                sum(r["category_id"] in [c for c, _ in r["top_two"]] for r in rows) / len(rows)
                if rows
                else 0.0
            ),
            "with_tie_break": _accuracy(rows, lambda r: _with_tie_break(r, tie_margin)),
            "tie_break_calls": sum(_margin(r) < tie_margin for r in rows),
        }

    # --- Retrieval (Day 2 acceptance gate) ---------------------------------
    retrieval = {
        "n": len(info),
        "hit_at_1": (
            sum(bool(r["sources"]) and r["sources"][0] == r["expected_source"] for r in info)
            / len(info)
            if info
            else 0.0
        ),
        "hit_at_k": (
            sum(r["expected_source"] in r["sources"] for r in info) / len(info) if info else 0.0
        ),
    }
    lexical_rows = [
        {
            "id": r["id"],
            "query": r["query"],
            "hit_at_1": bool(r["sources"]) and r["sources"][0] == r["expected_source"],
            "hit_at_k": r["expected_source"] in r["sources"],
        }
        for r in info
        if r.get("lexical")
    ]

    # --- Distributions and what the proposals would do ---------------------
    distributions = {
        "kb_dense/informational": _summary([r["top_dense_score"] for r in info]),
        "kb_dense/action": _summary([r["top_dense_score"] for r in action]),
        "kb_dense/out_of_scope": _summary([r["top_dense_score"] for r in oos]),
        "router_top1/in_scope": _summary([top1_score(r) for r in in_scope]),
        "router_top1/out_of_scope": _summary([top1_score(r) for r in oos]),
    }
    effects = {
        "info_at_or_above_high": cal.rate_at_or_above([r["top_dense_score"] for r in info], high),
        "info_at_or_above_mid": cal.rate_at_or_above([r["top_dense_score"] for r in info], mid),
        # Why the decision layer needs an "is this an action?" signal: action
        # requests match their articles closely and would clear the bar too.
        "action_at_or_above_high": cal.rate_at_or_above(
            [r["top_dense_score"] for r in action], high
        ),
        "oos_at_or_above_mid": cal.rate_at_or_above([r["top_dense_score"] for r in oos], mid),
        "in_scope_routed": cal.rate_at_or_above([top1_score(r) for r in in_scope], router_min),
        "oos_routed": cal.rate_at_or_above([top1_score(r) for r in oos], router_min),
    }

    # --- Latency ------------------------------------------------------------
    latency = {}
    for stage in ("embed", "search", "route", "tie_break"):
        values = [r["latency_ms"][stage] for r in results if r["latency_ms"].get(stage)]
        latency[stage] = {
            "n": len(values),
            "p50": cal.percentile(values, 50),
            "p95": cal.percentile(values, 95),
        }

    misrouted = [
        {
            "id": r["id"],
            "query": r["query"],
            "expected": r["category_id"],
            "top_two": r["top_two"],
            "llm_choice": r.get("llm_choice"),
        }
        for r in in_scope
        if _top1(r) != r["category_id"]
    ]

    return Analysis(
        n_rows=len(results),
        proposals=proposals,
        category=category,
        retrieval=retrieval,
        lexical_rows=lexical_rows,
        distributions=distributions,
        effects=effects,
        latency=latency,
        misrouted=misrouted,
    )


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render_markdown(a: Analysis, run_name: str, current: dict[str, float]) -> str:
    lines = [
        "# Eval report v0",
        "",
        f"Run: `{run_name}` · {a.n_rows} rows · generated by `scripts/eval_report.py`.",
        "",
        "## Proposed thresholds",
        "",
        "Proposals only — copy into `src/querydesk/core/config.py` after review.",
        "",
        "| Setting | Current | Proposed | Rule |",
        "| --- | --- | --- | --- |",
        f"| `confidence_high` | {current['confidence_high']:.2f} | "
        f"{a.proposals['confidence_high']:.2f} | above every out-of-scope KB score, "
        "and never below mid |",
        f"| `confidence_mid` | {current['confidence_mid']:.2f} | "
        f"{a.proposals['confidence_mid']:.2f} | keeps {_pct(INFO_KEEP_RATE)} of "
        "informational questions |",
        f"| `router_min_score` | {current['router_min_score']:.2f} | "
        f"{a.proposals['router_min_score']:.2f} | at most {_pct(OOS_ROUTED_MAX_RATE)} of "
        "out-of-scope messages get a category |",
        f"| `router_tie_margin` | {current['router_tie_margin']:.2f} | "
        f"{a.proposals['router_tie_margin']:.2f} | similarity alone is "
        f"≥ {_pct(TOP1_TARGET_ACCURACY)} right above it |",
        "",
        "What the proposals would do on this run:",
        "",
        f"- Informational questions at or above high / mid: "
        f"{_pct(a.effects['info_at_or_above_high'])} / {_pct(a.effects['info_at_or_above_mid'])}",
        f"- Out-of-scope messages at or above mid: {_pct(a.effects['oos_at_or_above_mid'])}",
        f"- In-scope routed / out-of-scope routed: {_pct(a.effects['in_scope_routed'])} / "
        f"{_pct(a.effects['oos_routed'])}",
        f"- **Action requests at or above high: {_pct(a.effects['action_at_or_above_high'])}.** "
        "Retrieval score alone can't tell an action from a question, so the decision layer "
        "(Day 4) needs its own action signal before rule 2 may answer.",
        "",
        "## Category accuracy",
        "",
        "| Population | n | Top-1 | Top-2 | With tie-break | Tie-break calls |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, label in (("in_scope", "In-scope"), ("sr_bound", "SR-bound (action)")):
        c = a.category[name]
        lines.append(
            f"| {label} | {c['n']} | {_pct(c['top1'])} | {_pct(c['top2'])} | "
            f"{_pct(c['with_tie_break'])} | {c['tie_break_calls']} |"
        )
    lines += [
        "",
        "PRD target: category accuracy on SR-bound queries ≥ 90%. "
        "A tie-break that returns no usable answer counts as wrong here (it escalates).",
        "",
        "## Retrieval — Day 2 acceptance gate",
        "",
        f"Informational questions citing the expected article: hit@1 "
        f"{_pct(a.retrieval['hit_at_1'])}, hit@k {_pct(a.retrieval['hit_at_k'])} "
        f"(n={a.retrieval['n']}).",
        "",
        "| Lexical row | Query | hit@1 | hit@k |",
        "| --- | --- | --- | --- |",
    ]
    for r in a.lexical_rows:
        lines.append(
            f"| {r['id']} | {r['query']} | {'✓' if r['hit_at_1'] else '✗'} | "
            f"{'✓' if r['hit_at_k'] else '✗'} |"
        )
    lines += [
        "",
        "## Score distributions",
        "",
        "| Score / population | n | min | median | max |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, d in a.distributions.items():
        lines.append(f"| {name} | {d['n']} | {d['min']:.3f} | {d['median']:.3f} | {d['max']:.3f} |")
    lines += [
        "",
        "## Latency (ms)",
        "",
        "| Stage | n | p50 | p95 |",
        "| --- | --- | --- | --- |",
    ]
    for stage, d in a.latency.items():
        if d["n"]:
            lines.append(f"| {stage} | {d['n']} | {d['p50']:.0f} | {d['p95']:.0f} |")
    lines += ["", "## Misrouted (top-1 wrong)", ""]
    if not a.misrouted:
        lines.append("None.")
    else:
        lines += [
            "| Row | Query | Expected | Top two | LLM pick |",
            "| --- | --- | --- | --- | --- |",
        ]
        for m in a.misrouted:
            top = ", ".join(f"{c} {s:.3f}" for c, s in m["top_two"])
            lines.append(
                f"| {m['id']} | {m['query']} | {m['expected']} | {top} | {m['llm_choice']} |"
            )
    return "\n".join(lines) + "\n"
