"""Calibration maths on synthetic scores — no eval run needed."""

import math

from querydesk.eval import calibration as cal


def test_rate_at_or_above_is_inclusive():
    assert cal.rate_at_or_above([0.2, 0.5, 0.5, 0.9], 0.5) == 0.75
    assert cal.rate_at_or_above([], 0.5) == 0.0


def test_limiting_threshold_sits_just_above_the_negatives():
    negatives = [0.10, 0.20, 0.31]
    # Allow none through: the bar must clear the highest negative.
    assert cal.lowest_threshold_limiting(negatives, max_rate=0.0) == 0.32
    # Allow a third through: the top negative may pass.
    assert cal.lowest_threshold_limiting(negatives, max_rate=1 / 3) == 0.21


def test_keeping_threshold_sits_at_the_positive_floor():
    positives = [0.55, 0.60, 0.70, 0.80]
    assert cal.highest_threshold_keeping(positives, min_rate=1.0) == 0.55
    assert cal.highest_threshold_keeping(positives, min_rate=0.75) == 0.60


def test_thresholds_handle_empty_inputs():
    assert cal.lowest_threshold_limiting([], 0.0) == 0.0
    assert cal.highest_threshold_keeping([], 0.9) == 0.0


def test_safe_margin_excludes_the_close_calls_that_were_wrong():
    rows = [(0.01, False), (0.02, False), (0.05, True), (0.10, True), (0.20, True)]
    # Everything at or above 0.03 is correct, so close calls below go to the LLM.
    assert cal.lowest_safe_margin(rows, target_accuracy=1.0) == 0.03


def test_safe_margin_is_zero_when_top1_is_always_right():
    rows = [(0.0, True), (0.01, True), (0.3, True)]
    assert cal.lowest_safe_margin(rows, target_accuracy=0.95) == 0.0


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert cal.percentile(values, 50) == 50
    assert cal.percentile(values, 95) == 95
    assert math.isnan(cal.percentile([], 50))


def test_grid_spans_zero_to_one_in_hundredths():
    assert cal.GRID[0] == 0.0 and cal.GRID[-1] == 1.0 and len(cal.GRID) == 101
    assert all(t == round(t, 2) for t in cal.GRID)
