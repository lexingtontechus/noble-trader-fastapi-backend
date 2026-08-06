"""
Tests for the Part II paper portfolio (regime_platform/sweep_orchestrator/paper_book.py).

Covers the pure sizing + PnL functions (no network):
  - compute_size: kelly sizing with cap, effective_kelly preference, fallback
  - compute_realized_pnl: $ PnL convention and r-multiple
  - PAPER_POSITION_COLUMNS completeness
"""

import pytest

from regime_platform.sweep_orchestrator.paper_book import (
    PAPER_POSITION_COLUMNS,
    compute_size,
    compute_realized_pnl,
)


class TestComputeSize:
    def test_effective_kelly_preferred(self):
        # effective_kelly 0.10 on base 1000 → notional 100 (10% of base)
        s = compute_size(entry_price=1.1000, kelly_f=0.05, effective_kelly=0.10)
        assert s["size_notional"] == pytest.approx(100.0, abs=0.01)
        assert s["size_units"] == pytest.approx(90.909, abs=0.01)

    def test_kelly_fallback_when_no_effective(self):
        s = compute_size(entry_price=1.1000, kelly_f=0.05)
        assert s["size_notional"] == pytest.approx(50.0, abs=0.01)

    def test_flat_fallback_when_neither(self):
        # No kelly → flat 5% of base = 50
        s = compute_size(entry_price=2.0000)
        assert s["size_notional"] == pytest.approx(50.0, abs=0.01)

    def test_hard_cap_20_percent(self):
        # kelly 1.0 would give 1000 — capped at 20% of base = 200
        s = compute_size(entry_price=1.0000, kelly_f=1.0, effective_kelly=1.0)
        assert s["size_notional"] == pytest.approx(200.0, abs=0.01)
        assert s["size_units"] == pytest.approx(200.0, abs=0.01)

    def test_kelly_clamped_to_unit_interval(self):
        s = compute_size(entry_price=1.0000, kelly_f=5.0)
        assert s["size_notional"] <= 200.0

    def test_zero_entry_units_zero(self):
        s = compute_size(entry_price=0.0, kelly_f=0.1)
        assert s["size_units"] == 0.0


class TestComputeRealizedPnl:
    def test_tp_hit_pnl(self):
        # 6-brick TP, brick 0.001, size_units 28.99 ($200 @ 6.9) → $0.174
        pnl = compute_realized_pnl(
            "tp_hit", pnl_bricks=6, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == pytest.approx(0.1739, abs=0.0005)
        assert pnl["r_multiple"] == pytest.approx(2.0, abs=0.0001)

    def test_sl_hit_pnl_negative(self):
        pnl = compute_realized_pnl(
            "sl_hit", pnl_bricks=-3, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == pytest.approx(-0.0870, abs=0.0005)
        assert pnl["r_multiple"] == pytest.approx(-1.0, abs=0.0001)

    def test_expired_zero_r(self):
        pnl = compute_realized_pnl(
            "expired", pnl_bricks=0, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == 0.0
        assert pnl["r_multiple"] == 0.0


class TestColumns:
    def test_columns_include_all_position_fields(self):
        for col in (
            "position_id", "signal_id", "symbol", "venue", "direction",
            "entry_price", "stop_loss", "take_profit",
            "size_units", "size_notional", "open_ts", "close_ts", "status",
            "exit_price", "realized_pnl", "pnl_bricks", "r_multiple",
            "source", "resolved_at",
        ):
            assert col in PAPER_POSITION_COLUMNS, f"missing column: {col}"
