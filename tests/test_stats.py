"""Tests for the exact Clopper-Pearson binomial CI used in the eval runner.

Verified against closed-form endpoints and reference values so the confidence
intervals in the README are trustworthy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.run_eval import clopper_pearson


def test_full_success_lower_bound_closed_form():
    # For k == n, lower bound = (alpha/2)^(1/n)
    lo, hi = clopper_pearson(40, 40)
    assert hi == 1.0
    assert abs(lo - 0.025 ** (1 / 40)) < 1e-6   # 0.9119...


def test_zero_success_upper_bound():
    lo, hi = clopper_pearson(0, 8)
    assert lo == 0.0
    assert abs(hi - (1 - 0.025 ** (1 / 8))) < 1e-6


def test_all_eight_lower_bound():
    lo, hi = clopper_pearson(8, 8)
    assert abs(lo - 0.025 ** (1 / 8)) < 1e-6     # 0.6306...
    assert hi == 1.0


def test_interval_contains_point_estimate():
    for k, n in [(12, 14), (26, 27), (4, 6), (3, 5)]:
        lo, hi = clopper_pearson(k, n)
        assert lo <= k / n <= hi


def test_reference_value_12_of_14():
    # R binom.test(12, 14)$conf.int -> [0.5718614, 0.9821535]
    lo, hi = clopper_pearson(12, 14)
    assert abs(lo - 0.5719) < 5e-3
    assert abs(hi - 0.9822) < 5e-3


def test_empty_returns_full_interval():
    assert clopper_pearson(0, 0) == (0.0, 1.0)
