"""Heavy-sweep resilience regression tests (2026-08-09).

Covers the fixes for the first real weekend run:
  1. Concurrency is capped (HEAVY_SWEEP_MAX_CONCURRENCY) — the naive
     "fire all 14 via asyncio.gather" exhausted the shared Redis pool
     (max_connections=10, retired 2026-08-25) → MaxConnectionsError on
     the last 4 symbols. The cap remains today to bound the CPU-bound
     grid-search thread pool.
  2. Failed / no-combo heavy sweeps leave a trace row (nullable `error`
     column in nt_sweep_result) instead of silently vanishing.

The original `timesfm:cached` Redis advisory-cache resilience tests were
removed here (2026-08-25) when that cache was deleted along with Redis —
they exercised a mock that the real code no longer calls, which would
have passed vacuously rather than testing anything.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from regime_platform.sweep_orchestrator.heavy_sweep import (
    HEAVY_SWEEP_MAX_CONCURRENCY,
    HeavySweepMixin,
    HeavySweepResult,
)


def _make_orchestrator(hmm_model=None):
    """Build a minimal HeavySweepMixin instance with only the attrs the
    group runner / per-symbol path touches."""
    orch = object.__new__(HeavySweepMixin)
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
    bound thread-pool pressure from the CPU-bound grid search."""
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
