"""
Build an eval report from an eval_run.py results file.

Prints the proposed thresholds and writes a markdown report — by default the
gitignored scratch file docs/eval-report-latest.md. Published reports
(docs/eval-report-v*.md) carry hand-written decisions, so this refuses to
overwrite one. It never edits config: copy the proposals into
src/querydesk/core/config.py yourself once you've read the report.

Usage:
    uv run scripts/eval_report.py                       # latest run
    uv run scripts/eval_report.py eval/results/<run>.jsonl
    uv run scripts/eval_report.py --out docs/eval-report-v2.md
"""

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script without an editable install (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from querydesk.core.config import BASE_DIR, settings  # noqa: E402
from querydesk.eval.report import analyse, render_markdown  # noqa: E402

RESULTS_DIR = BASE_DIR / "eval" / "results"
DEFAULT_OUT = BASE_DIR / "docs" / "eval-report-latest.md"


def main(results_path: Path | None, out: Path) -> None:
    if out.exists() and out.name.startswith("eval-report-v"):
        sys.exit(f"{out.name} is a published report with hand-written decisions — use a new --out.")

    if results_path is None:
        runs = sorted(RESULTS_DIR.glob("*.jsonl"))
        if not runs:
            sys.exit("No runs in eval/results — run scripts/eval_run.py first.")
        results_path = runs[-1]

    results = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
    analysis = analyse(results)
    current = {
        k: getattr(settings, k)
        for k in ("confidence_high", "confidence_mid", "router_min_score", "router_tie_margin")
    }

    out.write_text(render_markdown(analysis, results_path.name, current), encoding="utf-8")

    print("Proposed thresholds (current -> proposed):")
    for k, v in analysis.proposals.items():
        print(f"  {k:20} {current[k]:.2f} -> {v:.2f}")
    sr = analysis.category["sr_bound"]
    print(
        f"\nSR-bound category accuracy: top-1 {sr['top1']:.0%}, "
        f"with tie-break {sr['with_tie_break']:.0%}"
    )
    print(f"Informational source hit@k: {analysis.retrieval['hit_at_k']:.0%}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="?", type=Path, help="results .jsonl (default: latest)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="report path")
    args = parser.parse_args()
    main(args.results, args.out)
