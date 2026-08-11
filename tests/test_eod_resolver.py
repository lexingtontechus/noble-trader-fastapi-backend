"""
Tests for the EOD signal resolver (regime_platform/sweep_orchestrator/eod_resolver.py).

Covers the pure close-walk resolution logic and PnL convention:
  - BUY: tp_hit when close >= TP, sl_hit when close <= SL (TP checked first)
  - SELL: tp_hit when close <= TP, sl_hit when close >= SL
  - expired after EXPIRY_BARS with no touch
  - PnL convention: tp_hit = +tp_bricks, sl_hit = -sl_bricks, expired = 0
"""

import pytest

from regime_platform.sweep_orchestrator.eod_resolver import (
    resolve_signal_outcome,
    compute_pnl,
    EXPIRY_BARS,
    CALIBRATION_RECORD_COLUMNS,
    _write_calibration_record,
)


class TestCalibrationRecordPayload:
    """Regression (2026-08-10): nt_calibration_records inserts were failing
    with HTTP 400 / PostgreSQL 23502 (NOT NULL violation) because the payload
    omitted `id` (TEXT PRIMARY KEY, no DB default) and `aggression` (NOT NULL
    CHECK enum). Every EOD calibration insert failed → nt_calibration_records
    stayed empty → fetch_calibration_bias returned None → EOD correction
    wrote 0 actions → nt_eod_actions stayed empty → the recalibration loop
    had no data to act on."""

    def test_write_calibration_payload_has_id_and_aggression(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        captured = {}

        async def fake_insert(table, payload):
            captured["table"] = table
            captured["payload"] = payload
            return True

        sig = {
            "symbol": "BTCUSD",
            "venue": "Coinbase",
            "p_win": 0.62,
            "aggression": "aggressive",
            "entry_price": 60000.0,
            "ts": "2026-08-09T00:00:00Z",
        }
        resolved = {"outcome": "tp_hit", "exit_price": 61000.0, "hold_bars": 5}
        pnl = {"pnl_dollars": 100.0}

        async def run():
            with patch(
                "regime_platform.sweep_orchestrator.eod_resolver._insert_row",
                new=AsyncMock(side_effect=fake_insert),
            ):
                await _write_calibration_record(sig, resolved, pnl)

        asyncio.run(run())

        p = captured["payload"]
        assert captured["table"] == "nt_calibration_records"
        # id must be a non-empty string (TEXT PK NOT NULL — the 23502 fix)
        assert isinstance(p.get("id"), str) and len(p["id"]) > 0
        # aggression must be one of the CHECK enum values (NOT NULL fix)
        assert p.get("aggression") in ("passive", "mid", "aggressive")
        assert p.get("aggression") == "aggressive"
        # outcome mapped to win/loss
        assert p.get("outcome") == "win"
        assert p.get("symbol") == "BTCUSD"

    def test_write_calibration_payload_defaults_aggression_to_mid(self):
        """An invalid/absent aggression on the signal falls back to 'mid'
        so the NOT NULL CHECK constraint never rejects the row."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        captured = {}

        async def fake_insert(table, payload):
            captured["payload"] = payload
            return True

        sig = {
            "symbol": "EURUSD",
            "venue": "OANDA",
            "p_win": 0.55,
            # no aggression field
            "entry_price": 1.1000,
            "ts": "2026-08-09T00:00:00Z",
        }
        resolved = {"outcome": "sl_hit", "exit_price": 1.0950, "hold_bars": 3}
        pnl = {"pnl_dollars": -50.0}

        async def run():
            with patch(
                "regime_platform.sweep_orchestrator.eod_resolver._insert_row",
                new=AsyncMock(side_effect=fake_insert),
            ):
                await _write_calibration_record(sig, resolved, pnl)

        asyncio.run(run())

        p = captured["payload"]
        assert p.get("aggression") == "mid"
        assert p.get("outcome") == "loss"
        assert isinstance(p.get("id"), str) and len(p["id"]) > 0

    def test_calibration_columns_include_id_and_aggression(self):
        """The whitelist must pass id/aggression through (they were filtered
        out before, which is what made the payload incomplete)."""
        assert "id" in CALIBRATION_RECORD_COLUMNS
        assert "aggression" in CALIBRATION_RECORD_COLUMNS


class TestResolveSignalOutcome:
    def test_buy_tp_hit_before_sl(self):
        # entry 1.1000, TP 1.1100, SL 1.0900; closes rise into TP
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[1.1005, 1.1080, 1.1120], direction="buy",
        )
        assert result["outcome"] == "tp_hit"
        assert result["exit_idx"] == 2
        assert result["hold_bars"] == 3
        assert result["exit_price"] == pytest.approx(1.1120)
        assert result["mfe"] >= 0.012

    def test_buy_sl_hit_before_tp(self):
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[1.0990, 1.0880], direction="buy",
        )
        assert result["outcome"] == "sl_hit"
        assert result["exit_idx"] == 1
        assert result["exit_price"] == pytest.approx(1.0880)
        assert result["mae"] >= 0.012

    def test_buy_same_bar_tp_priority(self):
        # Close touches both TP and SL in the same bar — TP wins (checked first)
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[1.1200], direction="buy",
        )
        assert result["outcome"] == "tp_hit"

    def test_buy_expired(self):
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[1.1010, 1.1020, 1.1030], direction="buy",
        )
        assert result["outcome"] == "expired"
        assert result["exit_price"] is None
        assert result["exit_idx"] is None

    def test_buy_expired_honors_expiry_bars(self):
        # expiry_bars=2 → only first 2 closes walked → expired even though
        # the 3rd close would have hit TP
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[1.1005, 1.1010, 1.1120], direction="buy", expiry_bars=2,
        )
        assert result["outcome"] == "expired"
        assert result["hold_bars"] == 2

    def test_sell_tp_hit(self):
        # SELL: TP below entry, SL above. closes fall into TP
        result = resolve_signal_outcome(
            entry_price=1.2000, stop_loss=1.2100, take_profit=1.1900,
            closes=[1.1980, 1.1880], direction="sell",
        )
        assert result["outcome"] == "tp_hit"
        assert result["exit_price"] == pytest.approx(1.1880)

    def test_sell_sl_hit(self):
        result = resolve_signal_outcome(
            entry_price=1.2000, stop_loss=1.2100, take_profit=1.1900,
            closes=[1.2050, 1.2120], direction="sell",
        )
        assert result["outcome"] == "sl_hit"
        assert result["exit_price"] == pytest.approx(1.2120)

    def test_direction_short_is_sell(self):
        result = resolve_signal_outcome(
            entry_price=1.2000, stop_loss=1.2100, take_profit=1.1900,
            closes=[1.1880], direction="short",
        )
        assert result["outcome"] == "tp_hit"

    def test_empty_closes_expired(self):
        result = resolve_signal_outcome(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            closes=[], direction="buy",
        )
        assert result["outcome"] == "expired"
        assert result["hold_bars"] == 0

    def test_default_expiry_is_48(self):
        assert EXPIRY_BARS == 48


class TestComputePnl:
    def test_tp_hit_positive(self):
        pnl = compute_pnl("tp_hit", tp_bricks=6, sl_bricks=3, brick_size=0.001)
        assert pnl["pnl_bricks"] == 6.0
        assert pnl["pnl_dollars"] == pytest.approx(0.006)

    def test_sl_hit_negative(self):
        pnl = compute_pnl("sl_hit", tp_bricks=6, sl_bricks=3, brick_size=0.001)
        assert pnl["pnl_bricks"] == -3.0
        assert pnl["pnl_dollars"] == pytest.approx(-0.003)

    def test_expired_zero(self):
        pnl = compute_pnl("expired", tp_bricks=6, sl_bricks=3, brick_size=0.001)
        assert pnl["pnl_bricks"] == 0.0
        assert pnl["pnl_dollars"] == 0.0

    def test_negative_brick_counts_abs_guard(self):
        # Schema stores positive brick counts; abs() guards against negatives
        pnl = compute_pnl("sl_hit", tp_bricks=6, sl_bricks=-3, brick_size=0.001)
        assert pnl["pnl_bricks"] == -3.0
