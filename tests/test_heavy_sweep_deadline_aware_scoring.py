"""
Tests for the root-cause fix, worklog
20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md
(regime_platform/sweep_orchestrator/heavy_sweep.py:
``deadline_aware_sharpe`` / ``select_best_combo_deadline_aware``).

Both are pure functions — tested here with real ``ComboResult`` instances
and duck-typed trade dicts (only ``entry_ts``/``exit_ts``/``pnl_bricks``
are read, matching ``TradeRecord.to_dict()``'s shape), explicit
min_rr/max_sharpe/min_trades (not the module's live
config/feature_flags.yaml-derived defaults) so these tests never drift if
that config changes.
"""

import pytest

from regime_platform.sweep_orchestrator.heavy_sweep import (
    EXPIRY_BARS,
    ComboResult,
    deadline_aware_sharpe,
    select_best_combo_deadline_aware,
)

_FILTERS = dict(min_rr=1.0, max_sharpe=10.0, min_trades=1)


def _combo(brick_size, sharpe, deadline_aware_sharpe=0.0, sl_bricks=3, tp_bricks=6, total_trades=20):
    return ComboResult(
        brick_size=brick_size, sl_bricks=sl_bricks, tp_bricks=tp_bricks,
        sharpe=sharpe, total_trades=total_trades, win_rate=0.5,
        deadline_aware_sharpe=deadline_aware_sharpe,
    )


def _trade(entry_ts, exit_ts, pnl_bricks):
    return {"entry_ts": entry_ts, "exit_ts": exit_ts, "pnl_bricks": pnl_bricks}


# ─── deadline_aware_sharpe ─────────────────────────────────────────────


def test_deadline_aware_sharpe_empty_or_single_trade_is_zero():
    assert deadline_aware_sharpe([]) == 0.0
    assert deadline_aware_sharpe([_trade(0, 5, 6.0)]) == 0.0


def test_deadline_aware_sharpe_matches_uncapped_when_all_within_expiry():
    # All trades resolve well inside EXPIRY_BARS -> identical to a plain
    # mean/std(ddof=1) Sharpe over the raw pnls (no zeroing happens).
    trades = [_trade(0, 5, 6.0), _trade(10, 13, -3.0), _trade(20, 25, 6.0)]
    import numpy as np
    raw = np.array([6.0, -3.0, 6.0])
    expected = float(raw.mean() / raw.std(ddof=1))
    assert deadline_aware_sharpe(trades) == pytest.approx(expected)


def test_deadline_aware_sharpe_zeroes_trades_held_past_expiry():
    # Confirms EXPIRY_BARS is actually 48 (imported from eod_resolver, not
    # duplicated) and that a trade held longer gets scored as 0, not its
    # real (would-be) pnl.
    assert EXPIRY_BARS == 48
    short_trade = _trade(0, 10, 6.0)              # 10h held -> counts
    long_trade = _trade(0, EXPIRY_BARS + 1, 6.0)   # 49h held -> zeroed
    capped = deadline_aware_sharpe([short_trade, long_trade])
    uncapped = deadline_aware_sharpe([short_trade, _trade(0, 10, 6.0)])  # both short
    # With the long trade zeroed instead of +6, the two pnls (6, 0) have
    # a different mean/std than (6, 6) -> the results must differ, and
    # since (6,6) has zero variance (undefined/0.0 Sharpe by this
    # function's own zero-std guard) while (6,0) has real variance, the
    # capped case is the only one of the two producing a non-zero value.
    assert uncapped == 0.0   # std==0 across two identical +6 trades
    assert capped != 0.0


# ─── select_best_combo_deadline_aware ──────────────────────────────────


def test_off_mode_uses_uncapped_sharpe_no_comparison_fields():
    only = _combo(brick_size=1.0, sharpe=1.0, deadline_aware_sharpe=9.0)
    result = select_best_combo_deadline_aware([only], mode="off", **_FILTERS)
    assert result.chosen is only
    assert result.uncapped is None
    assert result.corrected is None
    assert result.changed is False


def test_shadow_mode_never_overrides_uncapped_even_when_corrected_is_better():
    uncapped_winner = _combo(brick_size=1.0, sharpe=5.0, deadline_aware_sharpe=0.1)
    corrected_winner = _combo(brick_size=2.0, sharpe=1.0, deadline_aware_sharpe=9.0)
    result = select_best_combo_deadline_aware(
        [uncapped_winner, corrected_winner], mode="shadow", **_FILTERS,
    )
    assert result.chosen is uncapped_winner        # zero live-behavior change
    assert result.uncapped is uncapped_winner
    assert result.corrected is corrected_winner    # logged, not applied
    assert result.changed is True


def test_live_mode_applies_corrected_pick():
    uncapped_winner = _combo(brick_size=1.0, sharpe=5.0, deadline_aware_sharpe=0.1)
    corrected_winner = _combo(brick_size=2.0, sharpe=1.0, deadline_aware_sharpe=9.0)
    result = select_best_combo_deadline_aware(
        [uncapped_winner, corrected_winner], mode="live", **_FILTERS,
    )
    assert result.chosen is corrected_winner
    assert result.uncapped is uncapped_winner
    assert result.corrected is corrected_winner
    assert result.changed is True


def test_live_mode_rescues_symbol_when_only_corrected_scoring_passes_filters():
    # The uncapped grid has nothing usable (e.g. its one candidate's
    # uncapped Sharpe blows the MAX_SHARPE outlier filter); the corrected
    # Sharpe for that same candidate is sane and passes.
    combo = _combo(brick_size=1.0, sharpe=99.0, deadline_aware_sharpe=1.5)
    result = select_best_combo_deadline_aware(
        [combo], mode="live", min_rr=1.0, max_sharpe=10.0, min_trades=1,
    )
    assert result.chosen is combo
    assert result.uncapped is None       # uncapped sharpe=99 > max_sharpe=10
    assert result.corrected is combo
    assert result.changed is True
