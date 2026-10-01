"""
Tests for regime_platform/sweep_orchestrator/hmm_shadow_accuracy.py,
worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md — outcome-linked evidence for
the Tier 1/2 live-mode decision.

``compute_tier1_accuracy``/``compute_tier2_brier``/``_realized_direction``
are pure functions (no I/O) — tested here against hand-computed expected
values.
"""

import asyncio

from pytest import approx as pytest_approx

import regime_platform.sweep_orchestrator.hmm_shadow_accuracy as hmm_shadow_accuracy
from regime_platform.sweep_orchestrator.hmm_shadow_accuracy import (
    _realized_direction,
    build_hmm_shadow_accuracy,
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


# ─── base_rate / skill (2026-10-01, RM-1 follow-up) ───────────────────────
# Accuracy alone is unreadable: the legacy HMM's 0.5251 looks marginally
# useful and is in fact BELOW the naive majority-direction baseline. These
# tests pin the metric that made that visible.


def test_skill_is_accuracy_minus_base_rate():
    # 4 decided rows: 3 realized DOWN / 1 UP -> base_rate = 0.75.
    # legacy makes 4 calls, 2 right -> accuracy 0.5, skill = 0.5 - 0.75 = -0.25.
    rows = [
        _row(signal="buy", outcome="sl_hit", legacy="DOWN"),   # realized DOWN (right)
        _row(signal="buy", outcome="sl_hit", legacy="UP"),     # realized DOWN (wrong)
        _row(signal="buy", outcome="sl_hit", legacy="UP"),     # realized DOWN (wrong)
        _row(signal="buy", outcome="tp_hit", legacy="UP"),     # realized UP   (right)
    ]
    result = compute_tier1_accuracy(rows)
    row = next(r for r in result if r["metric"] == "legacy")
    assert row["n"] == 4
    assert row["base_rate"] == pytest_approx(0.75)
    assert row["accuracy"] == pytest_approx(0.5)
    assert row["skill"] == pytest_approx(-0.25)


def test_negative_skill_when_below_naive_baseline():
    """The exact failure the RM-1 evaluation found: a model that is worse than
    'always predict the majority direction' must show NEGATIVE skill."""
    # 10 decided rows, 8 DOWN / 2 UP -> base_rate 0.8.
    rows = []
    for _ in range(8):
        rows.append(_row(signal="buy", outcome="sl_hit", legacy="DOWN"))  # realized DOWN
    for _ in range(2):
        rows.append(_row(signal="buy", outcome="tp_hit", legacy="UP"))    # realized UP
    # flip the legacy call on 6 of the 8 DOWN rows to UP -> those 6 are wrong
    for i in range(6):
        rows[i]["legacy_markov_state"] = "UP"
    result = compute_tier1_accuracy(rows)
    row = next(r for r in result if r["metric"] == "legacy")
    assert row["base_rate"] == pytest_approx(0.8)
    # calls: 6 UP(wrong) + 2 DOWN(right) + 2 UP(right) = 10 calls, 4 right
    assert row["accuracy"] == pytest_approx(0.4)
    assert row["skill"] == pytest_approx(-0.4)
    assert row["skill"] < 0


def test_positive_skill_when_beating_baseline():
    # 4 rows: 3 DOWN / 1 UP -> base 0.75. legacy calls DOWN on all 3 DOWN
    # rows and UP on the UP row -> accuracy 1.0, skill +0.25.
    rows = [
        _row(signal="buy", outcome="sl_hit", legacy="DOWN"),
        _row(signal="buy", outcome="sl_hit", legacy="DOWN"),
        _row(signal="buy", outcome="sl_hit", legacy="DOWN"),
        _row(signal="buy", outcome="tp_hit", legacy="UP"),
    ]
    result = compute_tier1_accuracy(rows)
    row = next(r for r in result if r["metric"] == "legacy")
    assert row["base_rate"] == pytest_approx(0.75)
    assert row["accuracy"] == pytest_approx(1.0)
    assert row["skill"] == pytest_approx(0.25)


def test_base_rate_uses_all_rows_not_just_this_sources_calls():
    """base_rate must reflect the symbol's whole decided population — a source
    that only calls 10% of the time should still be compared against the full
    base rate, not the base rate of its own (selected) subset."""
    rows = [_row(signal="buy", outcome="sl_hit", legacy=None) for _ in range(9)]
    rows.append(_row(signal="buy", outcome="tp_hit", legacy="UP"))
    result = compute_tier1_accuracy(rows)
    row = next(r for r in result if r["metric"] == "legacy")
    assert row["base_rate"] == pytest_approx(0.9)   # 9 DOWN / 1 UP over ALL rows
    assert row["coverage"] == pytest_approx(0.1)
    assert row["accuracy"] == pytest_approx(1.0)    # its one call was right
    assert row["skill"] == pytest_approx(0.1)       # 1.0 - 0.9


def test_skill_and_base_rate_null_when_no_decided_rows():
    """No realized direction -> no base rate -> no skill. Must be None, not 0.0
    (0.0 would read as 'exactly at baseline')."""
    rows = [_row(outcome="expired", legacy="UP")]
    assert compute_tier1_accuracy(rows) == []


def test_tier2_rows_carry_no_base_rate_or_skill():
    """Tier 2 is a Brier comparison, not a hit rate — base_rate/skill must stay
    absent so a reader cannot mistake a probability score for an accuracy one."""
    rows = [_row2(raw=0.8, gated=0.7)]
    result = compute_tier2_brier(rows)
    for r in result:
        assert "base_rate" not in r
        assert "skill" not in r


def test_skill_fields_present_on_every_tier1_row():
    rows = [_row(legacy="UP", trend_hmm="UP")]
    for r in compute_tier1_accuracy(rows):
        assert "base_rate" in r
        assert "skill" in r


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


# ─── build_hmm_shadow_accuracy stamps computed_at ──────────────────────────
# 2026-09-08 regression test: found live that the job re-ran and computed
# fresh rows (per the orchestrator log) but nt_hmm_shadow_accuracy.computed_at
# stayed frozen at the first-ever insert time on every subsequent run —
# compute_tier1_accuracy/compute_tier2_brier never set it, so the upsert
# payload omitted the column entirely, and an upsert-on-conflict never
# touches a column absent from the payload. Fixed by stamping computed_at
# centrally in build_hmm_shadow_accuracy, right before the upsert.


def test_build_hmm_shadow_accuracy_stamps_computed_at_on_every_row(monkeypatch):
    captured: dict[str, list] = {}

    async def fake_fetch_all(table, select, filters):
        return [_row(legacy="UP", trend_hmm="UP")]

    async def fake_upsert(rows):
        captured["rows"] = rows

    monkeypatch.setattr(hmm_shadow_accuracy, "_fetch_all", fake_fetch_all)
    monkeypatch.setattr(hmm_shadow_accuracy, "upsert_hmm_shadow_accuracy", fake_upsert)

    result = asyncio.run(build_hmm_shadow_accuracy())

    assert result, "expected at least one computed row"
    assert all("computed_at" in r and r["computed_at"] for r in result)
    # Same upsert call these rows were actually sent through — confirms
    # the stamp survives into the payload the DB actually receives, not
    # just the function's return value.
    assert all("computed_at" in r for r in captured["rows"])
