"""
Tests for regime_platform/sweep_orchestrator/hmm_shadow_accuracy.py,
worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md — outcome-linked evidence for
the Tier 1/2 live-mode decision.

``compute_tier1_accuracy``/``compute_tier2_brier``/``_realized_direction``
are pure functions (no I/O) — tested here against hand-computed expected
values.
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.hmm_shadow_accuracy import (
    _realized_direction,
    compute_tier1_accuracy,
    compute_tier2_brier,
)


# ─── _realized_direction ─────────────────────────────────────────────────


def test_realized_direction_buy_tp_hit_is_up():
    assert _realized_direction("buy", "tp_hit") == "UP"


def test_realized_direction_buy_sl_hit_is_down():
    assert _realized_direction("buy", "sl_hit") == "DOWN"


def test_realized_direction_sell_tp_hit_is_down():
    assert _realized_direction("sell", "tp_hit") == "DOWN"


def test_realized_direction_sell_sl_hit_is_up():
    assert _realized_direction("sell", "sl_hit") == "UP"


def test_realized_direction_unrecognized_inputs_return_none():
    assert _realized_direction("neutral", "tp_hit") is None
    assert _realized_direction("buy", "expired") is None


# ─── compute_tier1_accuracy ───────────────────────────────────────────────


def _row(symbol="AAPL", signal="buy", outcome="tp_hit", legacy=None, trend_hmm=None):
    return {
        "symbol": symbol, "signal": signal, "outcome": outcome,
        "legacy_markov_state": legacy, "trend_hmm_markov_state": trend_hmm,
    }


def test_tier1_accuracy_correct_and_incorrect_calls():
    # realized = UP (buy + tp_hit). legacy correctly called UP, trend_hmm
    # incorrectly called DOWN.
    rows = [_row(legacy="UP", trend_hmm="DOWN")]
    result = compute_tier1_accuracy(rows)
    by_metric = {r["metric"]: r for r in result}

    assert by_metric["legacy"]["n"] == 1
    assert by_metric["legacy"]["accuracy"] == pytest_approx(1.0)
    assert by_metric["legacy"]["coverage"] == pytest_approx(1.0)
    assert by_metric["trend_hmm"]["n"] == 1
    assert by_metric["trend_hmm"]["accuracy"] == pytest_approx(0.0)


def test_tier1_accuracy_flat_calls_excluded_from_accuracy_not_coverage():
    # 2 rows: one where legacy calls FLAT (no opinion), one where it
    # calls correctly. FLAT must not count as a "miss" in the accuracy
    # denominator, but both rows count toward total for coverage.
    rows = [
        _row(legacy="FLAT", trend_hmm="UP"),   # realized UP
        _row(legacy="UP", trend_hmm="UP"),     # realized UP
    ]
    result = compute_tier1_accuracy(rows)
    by_metric = {r["metric"]: r for r in result}

    # legacy: 1 non-FLAT call (the second row), both correct -> accuracy 1.0,
    # coverage 1/2 (only 1 of 2 total rows had a non-FLAT legacy call).
    assert by_metric["legacy"]["n"] == 1
    assert by_metric["legacy"]["accuracy"] == pytest_approx(1.0)
    assert by_metric["legacy"]["coverage"] == pytest_approx(0.5)
    # trend_hmm: 2 non-FLAT calls, both correct.
    assert by_metric["trend_hmm"]["n"] == 2
    assert by_metric["trend_hmm"]["accuracy"] == pytest_approx(1.0)
    assert by_metric["trend_hmm"]["coverage"] == pytest_approx(1.0)


def test_tier1_accuracy_missing_source_column_excluded_entirely():
    # legacy_markov_state is None (mode was off/live at capture time) —
    # must not appear as a "wrong" call, just absent from that source's n.
    rows = [_row(legacy=None, trend_hmm="UP")]  # realized UP
    result = compute_tier1_accuracy(rows)
    metrics = {r["metric"] for r in result}
    assert "legacy" not in metrics
    assert "trend_hmm" in metrics


def test_tier1_accuracy_sell_signal_direction():
    # sell + sl_hit -> realized UP (price moved against a sell).
    rows = [_row(signal="sell", outcome="sl_hit", legacy="UP", trend_hmm="DOWN")]
    result = compute_tier1_accuracy(rows)
    by_metric = {r["metric"]: r for r in result}
    assert by_metric["legacy"]["accuracy"] == pytest_approx(1.0)
    assert by_metric["trend_hmm"]["accuracy"] == pytest_approx(0.0)


def test_tier1_accuracy_per_symbol_isolation():
    rows = [
        _row(symbol="AAPL", legacy="UP", trend_hmm="UP"),      # realized UP, both correct
        _row(symbol="US500", legacy="DOWN", trend_hmm="DOWN"),  # realized UP, both wrong
    ]
    result = compute_tier1_accuracy(rows)
    by_symbol_metric = {(r["symbol"], r["metric"]): r for r in result}
    assert by_symbol_metric[("AAPL", "legacy")]["accuracy"] == pytest_approx(1.0)
    assert by_symbol_metric[("US500", "legacy")]["accuracy"] == pytest_approx(0.0)


# ─── compute_tier2_brier ──────────────────────────────────────────────────


def _row2(symbol="AAPL", outcome="tp_hit", raw=None, gated=None):
    return {"symbol": symbol, "outcome": outcome, "raw_p_regime": raw, "gated_p_regime": gated}


def test_tier2_brier_single_row_hand_computed():
    # tp_hit -> realized=1.0. raw=0.9 -> (0.9-1)^2=0.01. gated=0.5 -> (0.5-1)^2=0.25.
    rows = [_row2(raw=0.9, gated=0.5)]
    result = compute_tier2_brier(rows)
    by_metric = {r["metric"]: r for r in result}
    assert by_metric["raw"]["brier_score"] == pytest_approx(0.01)
    assert by_metric["gated"]["brier_score"] == pytest_approx(0.25)


def test_tier2_brier_averages_across_rows():
    rows = [
        _row2(outcome="tp_hit", raw=1.0),   # (1-1)^2 = 0.0
        _row2(outcome="sl_hit", raw=1.0),   # (1-0)^2 = 1.0
    ]
    result = compute_tier2_brier(rows)
    raw_row = next(r for r in result if r["metric"] == "raw")
    assert raw_row["n"] == 2
    assert raw_row["brier_score"] == pytest_approx(0.5)  # mean(0.0, 1.0)


def test_tier2_brier_missing_column_excluded():
    rows = [_row2(raw=0.8, gated=None)]
    result = compute_tier2_brier(rows)
    metrics = {r["metric"] for r in result}
    assert "raw" in metrics
    assert "gated" not in metrics


def test_tier2_brier_expired_rows_ignored():
    rows = [_row2(outcome="expired", raw=0.8)]
    result = compute_tier2_brier(rows)
    assert result == []
