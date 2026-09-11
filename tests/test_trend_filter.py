"""Tests for the trend filter (regime_platform/sweep_orchestrator/
trend_filter.py), worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md
SS5/SS7.

``compute_trend_regime``/``kelly_trend_multiplier`` are pure functions
(no I/O) — tested here against hand-computed/derived expected values
using config/feature_flags.yaml's shipped defaults (sma_short_days=10,
sma_mid_days=20, sma_long_days=50, rsi_period=14,
crossover_freshness_days=3, strength_saturation=0.10, boost_scale=0.3,
penalty_scale=0.5, rsi_overbought=70, rsi_oversold=30). The
Supabase-backed refresh/get_trend_regime functions are read-only
diagnostics, verified via a standalone real-data run instead — same
precedent as ``test_macro_filter.py``, which has no mocked-Supabase
suite for its own refresh/get functions.
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.trend_filter import (
    compute_trend_regime,
    kelly_trend_multiplier,
)

_SMA_LONG = 50
_FRESHNESS = 3
_MIN_BARS = max(_SMA_LONG, 14 + 1) + _FRESHNESS  # 53


# ─── compute_trend_regime ────────────────────────────────────────────────


def test_insufficient_history_returns_none():
    assert compute_trend_regime([100.0] * (_MIN_BARS - 1)) is None


def test_flat_price_is_trending_up_zero_strength_overbought_rsi():
    # A perfectly flat series ties SMA_short==SMA_long every day (sign
    # resolves to +1 on ties) and gives RSI=100 (zero deltas -> the
    # Wilder-smoothing avg_loss==0 branch) -- both deterministic edge
    # cases of the underlying formulas, not arbitrary choices.
    closes = [100.0] * (_MIN_BARS + 10)
    result = compute_trend_regime(closes)
    assert result["sma_10"] == pytest_approx(100.0)
    assert result["sma_50"] == pytest_approx(100.0)
    assert result["trend_strength"] == pytest_approx(0.0)
    assert result["rsi_14"] == pytest_approx(100.0)
    assert result["rsi_state"] == "overbought"
    assert result["last_crossover_direction"] == "bullish"
    # No flip anywhere in the lookback window -> not fresh.
    assert result["days_since_crossover"] == _FRESHNESS + 1
    assert result["trend_label"] == "trending_up"


def test_established_downtrend_is_trending_down():
    # Strictly decreasing closes: the trailing (short) window average is
    # always below the longer window average that still includes higher,
    # older prices -- sign is -1 every day, RSI trends toward oversold.
    n = _MIN_BARS + 10
    closes = [200.0 - i for i in range(n)]
    result = compute_trend_regime(closes)
    assert result["sma_10"] < result["sma_50"]
    assert result["last_crossover_direction"] == "bearish"
    assert result["days_since_crossover"] == _FRESHNESS + 1  # established, not fresh
    assert result["rsi_state"] == "oversold"
    assert result["trend_label"] == "trending_down"


def test_fresh_reversal_up_after_established_downtrend():
    # A declining baseline (SMA_short < SMA_long, sign=-1) for most of the
    # history, then a sharp 2-day spike at the very end large enough to
    # flip SMA_short above SMA_long -- a crossover strictly within the
    # 3-day freshness window. Modifying only the last 2 closes leaves
    # every earlier end_idx's SMA computation (which doesn't include
    # those indices) on the unmodified declining trend, so the earlier
    # offsets in the lookback stay solidly on the "-1" side.
    n = _MIN_BARS + 10
    closes = [200.0 - i * 0.5 for i in range(n)]
    closes[-1] += 100.0
    closes[-2] += 90.0

    result = compute_trend_regime(closes)
    assert result["last_crossover_direction"] == "bullish"
    assert result["days_since_crossover"] <= _FRESHNESS
    assert result["trend_label"] == "reversal_up"


def test_fresh_reversal_down_after_established_uptrend():
    n = _MIN_BARS + 10
    closes = [100.0 + i * 0.5 for i in range(n)]
    closes[-1] -= 100.0
    closes[-2] -= 90.0

    result = compute_trend_regime(closes)
    assert result["last_crossover_direction"] == "bearish"
    assert result["days_since_crossover"] <= _FRESHNESS
    assert result["trend_label"] == "reversal_down"


def test_strength_saturates_at_one():
    n = _MIN_BARS + 10
    closes = [100.0] * (n - 1) + [1000.0]  # extreme final spike
    result = compute_trend_regime(closes)
    assert result["trend_strength"] == pytest_approx(1.0)


def test_consolidating_when_no_crossover_and_rsi_neutral():
    # A symmetric +1/-1 day-over-day zig-zag around a constant mean: both
    # SMA_10 and SMA_50 converge to exactly the mean (no established
    # trend either side, no crossover in the lookback), and Wilder's RSI
    # locks onto exactly 50 (every step alternates an equal-magnitude
    # gain and loss, so avg_gain==avg_loss forever after warmup) — the
    # AAPL 2026-09-10 shape this module was built to catch (SS7): once a
    # reversal ages out of the freshness window with no fresh momentum
    # either direction, a flat/balanced tape reads consolidating, not
    # "still trending."
    n = _MIN_BARS + 10
    closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(n)]

    result = compute_trend_regime(closes)
    assert result["days_since_crossover"] == _FRESHNESS + 1  # not fresh
    # Warmup/parity edge effects keep this a couple points off dead-center
    # 50 rather than exact — the classification (not the precise value)
    # is what this test is really pinning down.
    assert result["rsi_14"] == pytest_approx(50.0, abs=5.0)
    assert result["rsi_state"] == "neutral"
    assert result["trend_label"] == "consolidating"


# ─── kelly_trend_multiplier ──────────────────────────────────────────────


def test_kelly_trend_multiplier_consolidating_is_noop():
    assert kelly_trend_multiplier("consolidating", 0.9, "buy") == pytest_approx(1.0)


def test_kelly_trend_multiplier_missing_label_is_noop():
    assert kelly_trend_multiplier(None, None, "buy") == pytest_approx(1.0)


def test_kelly_trend_multiplier_missing_strength_is_noop():
    assert kelly_trend_multiplier("trending_up", None, "buy") == pytest_approx(1.0)


def test_kelly_trend_multiplier_aligned_reversal_up_buy_boosts():
    mult = kelly_trend_multiplier("reversal_up", 1.0, "buy")
    assert mult == pytest_approx(1.0 + 1.0 * 0.3)  # boost_scale default 0.3


def test_kelly_trend_multiplier_aligned_trending_down_sell_boosts():
    mult = kelly_trend_multiplier("trending_down", 0.5, "sell")
    assert mult == pytest_approx(1.0 + 0.5 * 0.3)


def test_kelly_trend_multiplier_opposed_buy_penalizes():
    # direction=buy -> implied "bull"; label trending_down disagrees.
    mult = kelly_trend_multiplier("trending_down", 1.0, "buy")
    assert mult == pytest_approx(1.0 - 1.0 * 0.5)  # penalty_scale default 0.5


def test_kelly_trend_multiplier_opposed_sell_penalizes():
    mult = kelly_trend_multiplier("reversal_up", 0.4, "sell")
    assert mult == pytest_approx(1.0 - 0.4 * 0.5)


def test_kelly_trend_multiplier_clamps_strength_input_to_unit_range():
    assert kelly_trend_multiplier("reversal_up", 5.0, "buy") == pytest_approx(
        kelly_trend_multiplier("reversal_up", 1.0, "buy")
    )


def test_kelly_trend_multiplier_never_exceeds_hard_clamp_band(monkeypatch):
    import regime_platform.sweep_orchestrator.trend_filter as trend_filter_mod

    monkeypatch.setattr(trend_filter_mod, "get_trend_boost_scale", lambda: 5.0)
    monkeypatch.setattr(trend_filter_mod, "get_trend_penalty_scale", lambda: 5.0)

    assert kelly_trend_multiplier("reversal_up", 1.0, "buy") == pytest_approx(1.5)
    assert kelly_trend_multiplier("trending_down", 1.0, "buy") == pytest_approx(0.25)
