"""Unit tests for the hallucination-detection / confidence logic.

These are pure functions (result comparison, sanity checks, schema coverage,
confidence blending) and are the highest-value things to test after the
guardrails, so they're covered directly rather than only via the eval suite.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import validation
from app.executor import ExecutionResult


def _res(columns, rows):
    return ExecutionResult(ok=True, columns=columns, rows=rows, row_count=len(rows))


# ---- results_agree --------------------------------------------------------

def test_agree_identical():
    a = _res(["n"], [[5]])
    b = _res(["n"], [[5]])
    assert validation.results_agree(a, b)


def test_agree_order_insensitive():
    a = _res(["c", "n"], [["x", 1], ["y", 2]])
    b = _res(["c", "n"], [["y", 2], ["x", 1]])
    assert validation.results_agree(a, b)


def test_agree_float_tolerance():
    a = _res(["v"], [[1.0000001]])
    b = _res(["v"], [[1.0000002]])
    assert validation.results_agree(a, b)


def test_disagree_on_value():
    a = _res(["n"], [[5]])
    b = _res(["n"], [[6]])
    assert not validation.results_agree(a, b)


def test_disagree_on_rowcount():
    a = _res(["n"], [[5]])
    b = _res(["n"], [[5], [6]])
    assert not validation.results_agree(a, b)


def test_disagree_when_either_failed():
    a = _res(["n"], [[5]])
    bad = ExecutionResult(ok=False, error="boom")
    assert not validation.results_agree(a, bad)


# ---- schema_coverage ------------------------------------------------------

_SCHEMA = {
    "customers": {"columns": [{"name": "customer_id"}, {"name": "country"}]},
    "orders": {"columns": [{"name": "order_id"}, {"name": "customer_id"}]},
}


def test_coverage_all_present():
    score, problems = validation.schema_coverage(
        ["customers"], ["country", "orders.order_id"], _SCHEMA)
    assert score == 1.0
    assert problems == []


def test_coverage_flags_phantom_table_and_column():
    score, problems = validation.schema_coverage(
        ["employees"], ["salary"], _SCHEMA)
    assert score == 0.0
    assert any("employees" in p for p in problems)
    assert any("salary" in p for p in problems)


def test_coverage_partial():
    score, problems = validation.schema_coverage(
        ["customers", "ghost"], ["country"], _SCHEMA)
    assert 0.0 < score < 1.0
    assert len(problems) == 1


# ---- sanity_check ---------------------------------------------------------

def test_sanity_flags_empty_result():
    report = validation.sanity_check(_res(["n"], []))
    assert any("0 rows" in w for w in report.warnings)


def test_sanity_flags_null_heavy_column():
    rows = [[None], [None], [None], [1]]  # 75% NULL
    report = validation.sanity_check(_res(["c"], rows))
    assert any("NULL" in w for w in report.warnings)


def test_sanity_flags_out_of_range_dates():
    rows = [["2030-01-01"]]
    report = validation.sanity_check(_res(["d"], rows), data_date_range=("2024-01-01", "2026-06-30"))
    assert any("outside data range" in w for w in report.warnings)


def test_sanity_passes_clean_result():
    report = validation.sanity_check(_res(["country", "n"], [["India", 10], ["Japan", 8]]))
    assert report.score == 1.0
    assert not report.warnings


# ---- combine_confidence ---------------------------------------------------

def test_combine_all_signals_perfect():
    score, breakdown = validation.combine_confidence(1.0, 1.0, 1.0, True, 1.0)
    assert score == 1.0
    assert all(b["included"] for b in breakdown.values())


def test_combine_disagreement_lowers_score():
    agree, _ = validation.combine_confidence(0.9, 0.9, 1.0, True, 1.0)
    disagree, _ = validation.combine_confidence(0.9, 0.9, 1.0, False, 1.0)
    assert disagree < agree


def test_combine_skips_none_signal_and_reweights():
    """A skipped signal (deep validation off) redistributes its weight; the
    remaining active signals still produce a valid [0,1] score."""
    score, breakdown = validation.combine_confidence(1.0, 1.0, 1.0, None, 1.0)
    assert score == 1.0
    assert breakdown["agreement"]["included"] is False
    # active weights renormalise to 1.0
    active = [v["weight"] for v in breakdown.values() if v["included"]]
    assert abs(sum(active) - (1 - validation.WEIGHTS["agreement"])) < 1e-9


def test_combine_score_bounded():
    score, _ = validation.combine_confidence(0.0, 0.0, 0.0, False, 0.0)
    assert score == 0.0
