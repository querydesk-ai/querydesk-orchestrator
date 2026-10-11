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
    # Present only when the run recorded decision-layer output (Day 4+).
    decisions: dict | None = None


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
    # Runs without --tie-break never asked the LLM, so "with tie-break" can't be
    # measured (None), rather than counting every close call as a miss. Older
    # result files lack tie_break_ran; any recorded llm_choice means it ran.
    tie_break_ran = any(r.get("tie_break_ran", r.get("llm_choice") is not None) for r in results)
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
            "with_tie_break": (
                _accuracy(rows, lambda r: _with_tie_break(r, tie_margin)) if tie_break_ran else None
            ),
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
    for stage in ("embed", "search", "route", "tie_break", "decide", "extraction"):
        values = [r["latency_ms"][stage] for r in results if r["latency_ms"].get(stage) is not None]
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
        decisions=_decisions(results) if all("decision" in r for r in results) else None,
    )


ANSWERS = {"ANSWER", "ANSWER_WITH_DISCLAIMER"}
ROUTING_ACCURACY_TARGET = 0.90
ACTION_ANSWERED_MAX = 0.05


def _lenient_match(expected: str, actual: str) -> bool:
    # A disclaimer answer to an answerable question still answers it.
    return expected == actual or (expected == "ANSWER" and actual in ANSWERS)


def _decisions(results: list[dict]) -> dict:
    """PRD routing metrics: decision vs label, and the failure that matters most."""
    n = len(results)
    action = [r for r in results if r["kind"] == "action"]
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        row = confusion.setdefault(r["expected_decision"], {})
        row[r["decision"]] = row.get(r["decision"], 0) + 1

    def accuracy(rows, match):
        return sum(match(r["expected_decision"], r["decision"]) for r in rows) / len(rows)

    by_kind = {
        kind: accuracy(rows, _lenient_match)
        for kind in ("action", "informational", "out_of_scope")
        if (rows := [r for r in results if r["kind"] == kind])
    }
    return {
        "n": n,
        "strict": accuracy(results, str.__eq__),
        "lenient": accuracy(results, _lenient_match),
        "by_kind": by_kind,
        "action_answered": (
            sum(r["decision"] in ANSWERS for r in action) / len(action) if action else 0.0
        ),
        "confusion": confusion,
        "wrong": [
            {
                "id": r["id"],
                "query": r["query"],
                "expected": r["expected_decision"],
                "actual": r["decision"],
                "rule": r.get("rule"),
                "dense": r["top_dense_score"],
                "router": r["top_two"][0] if r["top_two"] else None,
                "missing": r.get("missing_fields") or [],
            }
            for r in results
            if not _lenient_match(r["expected_decision"], r["decision"])
        ],
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _render_decisions(d: dict) -> list[str]:
    ok = lambda hit: "✅" if hit else "❌"  # noqa: E731
    lines = [
        "## Decision layer",
        "",
        "| Metric | Result | PRD target |",
        "| --- | --- | --- |",
        f"| Routing accuracy (decision = label) | {_pct(d['lenient'])} "
        f"{ok(d['lenient'] >= ROUTING_ACCURACY_TARGET)} | ≥ {_pct(ROUTING_ACCURACY_TARGET)} |",
        f"| Routing accuracy, strict (disclaimer ≠ answer) | {_pct(d['strict'])} | — |",
        f"| Action requests answered with an article | {_pct(d['action_answered'])} "
        f"{ok(d['action_answered'] <= ACTION_ANSWERED_MAX)} | ≤ {_pct(ACTION_ANSWERED_MAX)} |",
    ]
    for kind, acc in d["by_kind"].items():
        lines.append(f"| … {kind.replace('_', '-')} rows | {_pct(acc)} | — |")
    labels = sorted({x for row in d["confusion"].values() for x in row} | set(d["confusion"]))
    lines += [
        "",
        "Confusion (rows = expected, columns = decided):",
        "",
        r"| Expected \ Decided | " + " | ".join(labels) + " |",
        "| --- |" + " --- |" * len(labels),
    ]
    for expected in labels:
        if expected in d["confusion"]:
            counts = d["confusion"][expected]
            lines.append(
                f"| {expected} | " + " | ".join(str(counts.get(c, "")) for c in labels) + " |"
            )
    lines += ["", "Wrong decisions:", ""]
    if not d["wrong"]:
        lines.append("None.")
    else:
        lines += [
            "| Row | Query | Expected | Decided | Rule | Dense | Router top-1 | Missing |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for w in d["wrong"]:
            router = f"{w['router'][0]} {w['router'][1]:.3f}" if w["router"] else "-"
            lines.append(
                f"| {w['id']} | {w['query']} | {w['expected']} | {w['actual']} | {w['rule']} | "
                f"{w['dense']:.3f} | {router} | {', '.join(w['missing'])} |"
            )
    return lines + [""]


def render_markdown(
    a: Analysis, run_name: str, current: dict[str, float], title: str = "Eval report"
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Run: `{run_name}` · {a.n_rows} rows · generated by `scripts/eval_report.py`.",
        "",
        *(_render_decisions(a.decisions) if a.decisions else []),
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
        "A tie-break that returns no usable answer counts as wrong here (it escalates); "
        "n/a means the run skipped it (re-run `eval_run.py --tie-break`).",
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
