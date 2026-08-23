"""
Tests for the EOD signal resolver (regime_platform/sweep_orchestrator/eod_resolver.py).

Covers the pure close-walk resolution logic and PnL convention:
  - BUY: tp_hit when close >= TP, sl_hit when close <= SL (TP checked first)
  - SELL: tp_hit when close <= TP, sl_hit when close >= SL
  - expired after EXPIRY_BARS with no touch
  - PnL convention: tp_hit = +tp_bricks, sl_hit = -sl_bricks, expired = 0
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.eod_resolver import (
    resolve_signal_outcome,
    compute_pnl,
    EXPIRY_BARS,
    CALIBRATION_RECORD_COLUMNS,
    _write_calibration_record,
    _resolve_one,
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

    def _write_payload(self, sig, resolved, pnl):
        import asyncio
        from unittest.mock import AsyncMock, patch

        captured = {}

        async def fake_insert(table, payload):
            captured["payload"] = payload
            return True

        async def run():
            with patch(
                "regime_platform.sweep_orchestrator.eod_resolver._insert_row",
                new=AsyncMock(side_effect=fake_insert),
            ):
                await _write_calibration_record(sig, resolved, pnl)

        asyncio.run(run())
        return captured["payload"]

    def test_calibration_payload_carries_raw_p_win(self):
        """predicted_p_win_raw (pre-enforcement) passes through the payload
        when the sim capture provided it (2026-08-13)."""
        sig = {
            "symbol": "EURUSD", "venue": "OANDA",
            "p_win": 0.62,          # enforced/shrunk value stored
            "predicted_p_win_raw": 0.91,  # raw pre-shrink value
            "entry_price": 1.1000, "ts": "2026-08-12T00:00:00Z",
        }
        p = self._write_payload(
            sig, {"outcome": "tp_hit", "exit_price": 1.1100, "hold_bars": 2},
            {"pnl_dollars": 80.0},
        )
        assert p.get("predicted_p_win") == 0.62
        assert p.get("predicted_p_win_raw") == 0.91

    def test_calibration_payload_brier_win_uses_raw(self):
        """Brier = (1 - raw)^2 on a win when the raw p_win is present."""
        sig = {
            "symbol": "EURUSD", "venue": "OANDA",
            "p_win": 0.60, "predicted_p_win_raw": 0.90,
            "entry_price": 1.1000, "ts": "2026-08-12T00:00:00Z",
        }
        p = self._write_payload(
            sig, {"outcome": "tp_hit", "exit_price": 1.1100, "hold_bars": 2},
            {"pnl_dollars": 80.0},
        )
        assert p.get("calibration_brier") == pytest.approx(0.01)  # (1-0.90)^2

    def test_calibration_payload_brier_loss_falls_back_to_p_win(self):
        """Without raw, Brier uses the stored (possibly enforced) p_win."""
        sig = {
            "symbol": "USDCAD", "venue": "OANDA",
            "p_win": 0.80,  # no predicted_p_win_raw
            "entry_price": 1.4000, "ts": "2026-08-12T00:00:00Z",
        }
        p = self._write_payload(
            sig, {"outcome": "sl_hit", "exit_price": 1.3950, "hold_bars": 3},
            {"pnl_dollars": -40.0},
        )
        assert p.get("calibration_brier") == pytest.approx(0.64)  # 0.80^2
        assert p.get("predicted_p_win_raw") is None

    def test_calibration_columns_include_raw_and_brier(self):
        assert "predicted_p_win_raw" in CALIBRATION_RECORD_COLUMNS
        assert "calibration_brier" in CALIBRATION_RECORD_COLUMNS


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


class TestResolveOneMinimumAgeGate:
    """Regression coverage for the 2026-08-23 premature-expiry fix
    (worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md root cause 3):
    a signal with fewer than EXPIRY_BARS post-entry closes available must
    NOT be force-resolved as 'expired' — it should stay pending until it
    either hits TP/SL or genuinely accumulates the full window. Confirmed
    live: US500 hold_bars 2-3, AAPL 1-9 — nowhere near EXPIRY_BARS=48.
    """

    def _sig(self, **overrides):
        base = {
            "signal_id": "sig-1", "symbol": "AAPL", "signal": "buy",
            "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
            "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
            "ts": "2026-08-23T00:00:00Z",
        }
        base.update(overrides)
        return base

    def test_few_bars_no_touch_stays_pending_not_expired(self):
        """3 bars available (like the live US500 case), none touching
        TP/SL — must return 'pending', NOT be patched as 'expired'."""
        patch_mock = AsyncMock(return_value=True)
        calib_mock = AsyncMock()
        with (
            patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock),
            patch("regime_platform.sweep_orchestrator.eod_resolver._write_calibration_record", new=calib_mock),
        ):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=[101.0, 102.0, 103.0]))
        assert outcome == "pending"
        patch_mock.assert_not_awaited()  # nt_signal_sim row must NOT be touched
        calib_mock.assert_not_awaited()

    def test_genuine_expiry_at_full_window_still_resolves(self):
        """A signal that HAS accumulated the full EXPIRY_BARS window and
        still never touched TP/SL must still resolve as a genuine 'expired'
        — the gate must not disable real expiry entirely."""
        patch_mock = AsyncMock(return_value=True)
        calib_mock = AsyncMock()
        closes = [101.0] * EXPIRY_BARS  # never reaches TP=110 or SL=95
        with (
            patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock),
            patch("regime_platform.sweep_orchestrator.eod_resolver._write_calibration_record", new=calib_mock),
        ):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=closes))
        assert outcome == "expired"
        patch_mock.assert_awaited_once()
        args, kwargs = patch_mock.call_args
        assert args[1]["outcome"] == "expired"
        calib_mock.assert_not_awaited()  # expired never writes a calibration record

    def test_tp_hit_within_few_bars_resolves_immediately(self):
        """A real TP touch must resolve right away even with very few bars
        available — the gate only defers a would-be 'expired' verdict, it
        must never delay a genuine win/loss."""
        patch_mock = AsyncMock(return_value=True)
        calib_mock = AsyncMock()
        with (
            patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock),
            patch("regime_platform.sweep_orchestrator.eod_resolver._write_calibration_record", new=calib_mock),
        ):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=[105.0, 111.0]))
        assert outcome == "tp_hit"
        patch_mock.assert_awaited_once()
        calib_mock.assert_awaited_once()

    def test_sl_hit_within_few_bars_resolves_immediately(self):
        patch_mock = AsyncMock(return_value=True)
        calib_mock = AsyncMock()
        with (
            patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock),
            patch("regime_platform.sweep_orchestrator.eod_resolver._write_calibration_record", new=calib_mock),
        ):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=[97.0, 94.0]))
        assert outcome == "sl_hit"
        patch_mock.assert_awaited_once()
        calib_mock.assert_awaited_once()

    def test_exactly_expiry_bars_with_no_touch_is_genuine_expiry(self):
        """Boundary: len(closes) == EXPIRY_BARS (not less) must resolve as
        real 'expired', not 'pending' — off-by-one check on the gate."""
        patch_mock = AsyncMock(return_value=True)
        with patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=[101.0] * EXPIRY_BARS))
        assert outcome == "expired"

    def test_one_bar_short_of_expiry_stays_pending(self):
        """Boundary: EXPIRY_BARS - 1 closes, no touch — still 'pending'."""
        patch_mock = AsyncMock(return_value=True)
        with patch("regime_platform.sweep_orchestrator.eod_resolver._patch_signal", new=patch_mock):
            outcome = asyncio.run(_resolve_one(self._sig(), closes=[101.0] * (EXPIRY_BARS - 1)))
        assert outcome == "pending"
        patch_mock.assert_not_awaited()


class TestResolvePendingSignalsInsufficientBarsCounter:
    def test_insufficient_bars_counted_separately_from_resolved_and_errors(self):
        from regime_platform.sweep_orchestrator import eod_resolver as mod

        pending_sig = {
            "signal_id": "sig-1", "symbol": "AAPL", "signal": "buy",
            "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
            "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
            "ts": "2026-08-23T00:00:00Z", "tradingview_symbol": "NASDAQ:AAPL",
        }
        fetch_pending_mock = AsyncMock(side_effect=[[pending_sig], []])
        active_symbols_mock = AsyncMock(return_value=[{"symbol": "AAPL", "tradingview_symbol": "NASDAQ:AAPL"}])
        candles_mock = AsyncMock(return_value={"AAPL": [101.0, 102.0, 103.0]})  # only 3 bars, no touch

        with (
            patch.object(mod, "_fetch_pending", new=fetch_pending_mock),
            patch.object(mod, "fetch_candles_for_symbols", new=candles_mock),
            # Bypass real-time entry-offset windowing — not under test here;
            # this test is about the insufficient_bars counter plumbing.
            patch.object(mod, "anchor_post_entry_closes", side_effect=lambda closes, *a, **kw: list(closes)),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=active_symbols_mock,
            ),
            patch.object(mod.settings, "SUPABASE_URL", "https://x.supabase.co"),
        ):
            summary = asyncio.run(mod.resolve_pending_signals(limit=10))

        assert summary["insufficient_bars"] == 1
        assert summary["resolved"] == 0
        assert summary["errors"] == 0


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
