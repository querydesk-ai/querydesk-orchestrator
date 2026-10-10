"""
Threshold calibration (CLAUDE.md invariant #4: thresholds are calibrated, not
guessed). Pure functions over score lists, so they're unit-testable and the
scripts that call them stay thin.

Every threshold is picked on a fixed 0.00–1.00 grid at 0.01 steps: coarse
enough that a 60-row eval set can't overfit to the third decimal, fine enough
for cosine similarities.
"""

import math

GRID = [round(i / 100, 2) for i in range(101)]


def rate_at_or_above(scores: list[float], threshold: float) -> float:
    """Fraction of scores >= threshold. 0.0 for an empty list."""
    if not scores:
        return 0.0
    return sum(s >= threshold for s in scores) / len(scores)


def lowest_threshold_limiting(negatives: list[float], max_rate: float) -> float:
    """
    The lowest threshold at which at most `max_rate` of the negatives score
    at or above it — i.e. how high the bar must be to keep the wrong things out.
    """
    for t in GRID:
        if rate_at_or_above(negatives, t) <= max_rate:
            return t
    return GRID[-1]


def highest_threshold_keeping(positives: list[float], min_rate: float) -> float:
    """
    The highest threshold that still keeps at least `min_rate` of the
    positives at or above it — i.e. how low the bar must be to let the right
    things through.
    """
    for t in reversed(GRID):
        if rate_at_or_above(positives, t) >= min_rate:
            return t
    return GRID[0]


def lowest_safe_margin(rows: list[tuple[float, bool]], target_accuracy: float) -> float:
    """
    Router tie margin. `rows` are (margin between top-1 and top-2, top-1 was
    correct). Returns the lowest margin m such that every row with margin >= m
    is top-1 correct at `target_accuracy` or better. Rows below m are the close
    calls that get sent to the LLM tie-break.
    """
    for m in GRID:
        kept = [correct for margin, correct in rows if margin >= m]
        if not kept or sum(kept) / len(kept) >= target_accuracy:
            return m
    return GRID[-1]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0–100). NaN for an empty list."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]
