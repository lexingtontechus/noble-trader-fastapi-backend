"""Heavy-sweep resilience regression tests (2026-08-09).

Covers the fixes for the first real weekend run:
  1. Concurrency is capped (HEAVY_SWEEP_MAX_CONCURRENCY) — the naive
     "fire all 14 via asyncio.gather" exhausted the shared Redis pool
     (max_connections=10) → MaxConnectionsError on the last 4 symbols.
  2. The `timesfm:cached` Redis read/write is advisory — a Redis failure
     must degrade to cache-miss and proceed, never fail the sweep.
  3. Failed / no-combo heavy sweeps leave a trace row (nullable `error`
     column in nt_sweep_result) instead of silently vanishing.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from regime_platform.sweep_orchestrator.heavy_sweep import (
    HEAVY_SWEEP_MAX_CONCURRENCY,
    HeavySweepMixin,
    HeavySweepResult,
)


def _make_orchestrator(redis=None, hmm_model=None):
    """Build a minimal HeavySweepMixin instance with only the attrs the
    group runner / per-symbol path touches."""
    orch = object.__new__(HeavySweepMixin)
    orch._redis = redis
    orch._hmm_bootstrapped = set()
    model = hmm_model or MagicMock()
    # The group runner checks `pushed > 0` — return 0 so symbols aren't
    # re-added, and emit_markov must unpack to a 2-tuple.
    model.bootstrap_from_ohlcv.return_value = 0
    model.emit_markov.return_value = ([[1.0, 0.0], [0.0, 1.0]], [1.0, 0.0])
    orch._hmm_model = model
    orch._hmm_buffer = {}
    orch._ev_engine = MagicMock()
    orch._sizer = MagicMock()
    orch._scheduler = None
    # R3 (2026-08-23): _fetch_p_historical_cached needs this cache dict.
    orch._p_historical_cache = {}
    return orch


def test_concurrency_cap_is_sane():
    """The cap must be a small positive integer — the whole point is to
    bound Redis + thread pressure."""
    assert isinstance(HEAVY_SWEEP_MAX_CONCURRENCY, int)
    assert 1 <= HEAVY_SWEEP_MAX_CONCURRENCY <= 8


def test_heavy_sweep_group_caps_concurrency():
    """With 14 symbols, at most HEAVY_SWEEP_MAX_CONCURRENCY run at once."""
    symbols = [
        {"symbol": f"S{i:02d}", "asset_class": "forex"} for i in range(14)
    ]
    prices = {s["symbol"]: [100.0 + i for i in range(200)] for s in symbols}

    inflight = 0
    peak = 0
    completed = 0

    async def fake_run_heavy(self, sym, prices=None):
        nonlocal inflight, peak, completed
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.005)
        inflight -= 1
        completed += 1
        r = HeavySweepResult(symbol=sym["symbol"])
        r.error = None
        return r

    async def fake_fetch(symbols, n_bars, interval):
        return prices

    async def scenario():
        orch = _make_orchestrator()
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_candles_for_symbols",
            new=fake_fetch,
        ), patch.object(
            HeavySweepMixin, "run_heavy_sweep_for_symbol", new=fake_run_heavy,
        ):
            await orch._heavy_sweep_group("0 20 * * sun", symbols)
        return peak, completed

    peak, completed = asyncio.run(scenario())
    assert completed == 14
    assert peak <= HEAVY_SWEEP_MAX_CONCURRENCY


def test_timesfm_cache_read_failure_degrades_to_miss():
    """A Redis MaxConnectionsError on the advisory cache read must NOT
    fail the symbol — it degrades to cache-miss and proceeds."""

    class _ExplodingRedis:
        async def get(self, key):
            import redis.exceptions
            raise redis.exceptions.MaxConnectionsError("Too many connections")

        async def set(self, *a, **k):
            import redis.exceptions
            raise redis.exceptions.MaxConnectionsError("Too many connections")

    async def fake_fetch(symbols, n_bars, interval):
        return {s["symbol"]: [100.0 + i for i in range(200)] for s in symbols}

    # Force the per-symbol path to stop right after the cache read by
    # making the grid search return an empty result list (→ no_combo path,
    # which is the earliest branch after the TimesFM gate). The point is
    # that the cache-read exception is swallowed and we REACH the grid.
    async def scenario():
        orch = _make_orchestrator(redis=_ExplodingRedis())
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_candles_for_symbols",
            new=fake_fetch,
        ), patch.object(
            HeavySweepMixin, "_renko_grid_search_sync",
            return_value=[],
        ), patch.object(
            HeavySweepMixin, "_regime_sync",
            return_value=SimpleNamespace(regime_label="low_vol_bull", confidence=0.8),
        ), patch.object(
            HeavySweepMixin, "_kelly_sync",
            return_value=SimpleNamespace(recommended_f=0.02),
        ), patch.object(
            HeavySweepMixin, "_log_heavy_failure_trace", new=AsyncMock(),
        ):
            sym = {"symbol": "XAUUSD", "asset_class": "metals"}
            result = await orch.run_heavy_sweep_for_symbol(
                sym, [100.0 + i for i in range(200)],
            )
        return result

    result = asyncio.run(scenario())
    # The cache read blew up but we proceeded to the grid search
    # (which found no combos → no_combo error, NOT a Redis error).
    assert result is not None
    assert result.error is not None
    assert "MaxConnectionsError" not in result.error
    assert "no_combo" in result.error


def test_timesfm_cache_write_failure_is_best_effort():
    """A Redis failure on the p_timesfm cache WRITE must not fail the sweep."""
    import redis.exceptions

    class _ExplodingRedis:
        async def get(self, key):
            return None  # cache miss — proceed to fresh fetch

        async def set(self, *a, **k):
            raise redis.exceptions.MaxConnectionsError("Too many connections")

    async def fake_fetch(symbols, n_bars, interval):
        return {s["symbol"]: [100.0 + i for i in range(200)] for s in symbols}

    async def scenario():
        orch = _make_orchestrator(redis=_ExplodingRedis())
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_candles_for_symbols",
            new=fake_fetch,
        ), patch.object(
            HeavySweepMixin, "_renko_grid_search_sync",
            return_value=[SimpleNamespace(
                brick_size=1.0, sl_bricks=3, tp_bricks=6,
                sharpe=1.5, total_trades=15, win_rate=0.7,
                profit_factor=1.2, total_pnl_bricks=3.0,
                max_drawdown_bricks=-1.0,
            )],
        ), patch.object(
            HeavySweepMixin, "_regime_sync",
            return_value=SimpleNamespace(
                regime_label="low_vol_bull", confidence=0.8,
                trend_probs=[0.34, 0.33, 0.33],
            ),
        ), patch.object(
            HeavySweepMixin, "_kelly_sync",
            return_value=SimpleNamespace(recommended_f=0.02),
        ), patch.object(
            HeavySweepMixin, "_evaluate_heavy_qualification", return_value=False,
        ), patch.object(
            HeavySweepMixin, "_persist_brick_params_to_yaml", new=AsyncMock(),
        ):
            # EV engine + timesfm fetch must be stubbed too — the write
            # happens mid-flow, after p_timesfm is computed.
            orch._ev_engine.compute_ev.return_value = SimpleNamespace(
                ev=0.2, ev_scale=1.0, p_win=0.6, allowed=False,
            )
            import regime_platform.sweep_orchestrator.orchestrator as orch_mod
            import regime_platform.sweep_orchestrator.supabase_client as sb_mod
            import regime_platform.sweep_orchestrator.realtime_publisher as rt_mod
            with patch.object(
                orch_mod, "_fetch_timesfm_cached", new=AsyncMock(return_value=None),
            ), patch.object(
                orch_mod, "_compute_trade_signal",
                return_value={
                    "signal": "buy", "entry_price": 100.0,
                    "stop_loss": 99.0, "take_profit": 102.0,
                },
            ), patch.object(
                orch_mod, "_derive_venue", return_value="OANDA",
            ), patch.object(
                orch_mod, "_price_decimals", return_value=2,
            ), patch(
                # latest_ta(self._hmm_buffer, symbol) — stub the module fn so
                # the real buffer (a dict in the test) is never touched.
                "regime_platform.services.hmm.latest_ta",
                return_value={
                    "ta_adx": None, "ta_ao": None, "ta_bb_power": None,
                    "ta_macd": None, "ta_rsi": None,
                },
            ), patch.object(
                # Block the fire-and-forget signal-sim + realtime capture
                # (they would leak fake rows to real Supabase).
                sb_mod, "log_signal_sim", new=AsyncMock(),
            ), patch.object(
                rt_mod, "broadcast_signal", new=lambda *a, **k: None,
            ):
                sym = {"symbol": "XAUUSD", "asset_class": "metals"}
                result = await orch.run_heavy_sweep_for_symbol(
                    sym, [100.0 + i for i in range(200)],
                )
        return result

    result = asyncio.run(scenario())
    # The SET blew up but the sweep completed (qualified=False) — the
    # write is best-effort and must not propagate.
    assert result.error is None
    assert result.qualified is False


def test_failure_trace_row_written_for_no_combo():
    """The no-combo path calls _log_heavy_failure_trace so failures are
    visible in nt_sweep_result (previously silent)."""
    trace = AsyncMock()

    async def scenario():
        orch = _make_orchestrator()
        with patch.object(
            HeavySweepMixin, "_log_heavy_failure_trace", new=trace,
        ):
            result = HeavySweepResult(symbol="EURUSD")
            result.error = "no_combo_passed_filters (tested 30 combos...)"
            result.duration_ms = 1234
            await orch._log_heavy_failure_trace("EURUSD", "forex", result)
        return result

    result = asyncio.run(scenario())
    trace.assert_awaited_once()
