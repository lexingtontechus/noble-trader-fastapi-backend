"""Tests for TradeJournal.get_stats() — specifically the 2026-08-24
profit_factor fix.

Regression for a live incident: a US500 emergency heavy sweep produced a
backtest window with zero losing trades (win_rate=1.0), so
profit_factor = gross_profit / gross_loss hit the `gross_loss == 0` branch,
which previously returned literal float("inf"). Postgres/JSON reject
Infinity ("Out of range float values are not JSON compliant"), so the
entire nt_sweep_result row failed to insert for that tick.
"""

import math

from renko.pattern_detector import PatternType, SignalDirection
from renko.trade_journal import TradeJournal, TradeRecord, _PROFIT_FACTOR_CAP


def _make_trade(pnl_bricks: float, trade_id: str = "t") -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        symbol="US500",
        direction=SignalDirection.LONG,
        pattern_type=PatternType.BULL_RUN,
        pnl_bricks=pnl_bricks,
    )


class TestProfitFactorNoLosses:
    def test_zero_losses_returns_finite_cap_not_inf(self):
        journal = TradeJournal()
        for i in range(10):
            journal.trades.append(_make_trade(1.0, f"t{i}"))
        stats = journal.get_stats()
        assert stats["win_rate"] == 1.0
        assert stats["profit_factor"] == _PROFIT_FACTOR_CAP
        assert math.isfinite(stats["profit_factor"])

    def test_zero_losses_result_is_json_serializable(self):
        """The actual failure mode: a payload containing this value must
        round-trip through json.dumps (Postgres/JSON reject Infinity)."""
        import json

        journal = TradeJournal()
        for i in range(5):
            journal.trades.append(_make_trade(2.0, f"t{i}"))
        stats = journal.get_stats()
        # Would raise ValueError before the fix (allow_nan=False mirrors
        # what a strict JSON encoder / Postgres does).
        json.dumps(stats, allow_nan=False)

    def test_mixed_wins_and_losses_unaffected(self):
        """The normal case (some losses) must still compute the real
        ratio, not the cap."""
        journal = TradeJournal()
        journal.trades.append(_make_trade(4.0, "win"))
        journal.trades.append(_make_trade(-2.0, "loss"))
        stats = journal.get_stats()
        assert stats["profit_factor"] == 2.0

    def test_all_losses_is_zero_not_cap(self):
        """gross_profit=0 with real losses -> 0.0, a legitimate finite
        ratio, not the no-losses cap (which is a distinct edge case)."""
        journal = TradeJournal()
        journal.trades.append(_make_trade(-1.0, "loss1"))
        journal.trades.append(_make_trade(-2.0, "loss2"))
        stats = journal.get_stats()
        assert stats["profit_factor"] == 0.0
