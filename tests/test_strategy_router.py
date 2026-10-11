"""Strategy Router R1 — deterministic shadow routing tests.

Pins the routing contract R2's measurement depends on. The router is a pure
function of (symbol, direction, regime, macro, trend) + the R0 catalog, so
these are deterministic, no-I/O assertions.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.strategy_router import (
    RoutedStrategy,
    _direction_word,
    _opposes,
    _vol_tier,
    route,
)


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_vol_tier_extraction():
    assert _vol_tier("low_vol_bull") == "low_vol"
    assert _vol_tier("med_high_vol_strong_bear") == "med_high_vol"
    assert _vol_tier("med_low_vol_bull") == "med_low_vol"
    assert _vol_tier("high_vol_bear") == "high_vol"
    assert _vol_tier(None) is None
    assert _vol_tier("unknown") is None


def test_direction_word():
    assert _direction_word("buy") == "buy"
    assert _direction_word("SELL") == "sell"
    assert _direction_word("neutral") is None
    assert _direction_word(None) is None


def test_opposes_both_required():
    # Gold-buy-into-downtrend: macro=bear AND trend=down both oppose a buy.
    assert _opposes("buy", "bear", "trending_down") is True
    assert _opposes("buy", "bear", "reversal_down") is True
    # Aligned: macro=bull agrees with a buy.
    assert _opposes("buy", "bull", "trending_up") is False
    # Mixed (one opposes, one agrees) -> not "opposes both".
    assert _opposes("buy", "bear", "trending_up") is False
    # A missing/neutral read is not evidence: only one read available and it
    # opposes -> still "opposes" (every available read opposes).
    assert _opposes("buy", "bear", None) is True
    assert _opposes("buy", None, "trending_down") is True
    # No usable read at all -> None (can't judge).
    assert _opposes("buy", None, None) is None
    assert _opposes("buy", "neutral", None) is None


# ── Routing (against the real R0 catalog) ───────────────────────────────────

def test_gold_buy_into_downtrend_routes_mean_reversion():
    """The ID45 pattern: XAUUSD buy, macro=bear + trend=down (both oppose),
    low/med-low vol regime -> mean_reversion (the era-clean 38% cohort)."""
    r = route(
        symbol="XAUUSD", signal="buy",
        regime_label="low_vol_bull",  # vol tier low_vol, in MR's regime_vol
        asset_class="metal", macro_label="bear", trend_label="trending_down",
    )
    assert r.matched is True
    assert r.archetype == "mean_reversion"
    assert r.trade_shape is not None
    assert r.trade_shape["exit"]["target"] == "mean"
    assert r.trade_shape["risk"]["overshoot_stop_z"] == 3.5


def test_silver_buy_aligned_routes_momentum():
    """XAGUSD buy aligned with macro/trend -> momentum (silver trend-continues,
    the expected-sign cohort). NOT swallowed by mean_reversion."""
    r = route(
        symbol="XAGUSD", signal="buy",
        regime_label="low_vol_bull", asset_class="metal",
        macro_label="bull", trend_label="trending_up",
    )
    assert r.matched is True
    assert r.archetype == "momentum"
    assert r.trade_shape["exit"]["target"] == "far"
    assert r.trade_shape["risk"]["overshoot_stop_z"] is None


def test_btc_sell_aligned_routes_momentum():
    r = route(
        symbol="BTCUSD", signal="sell",
        regime_label="high_vol_bear", asset_class="crypto",
        macro_label="bear", trend_label="trending_down",
    )
    assert r.archetype == "momentum"


def test_mean_reversion_is_metal_buy_only():
    """A counter-trend EURUSD buy does NOT get mean_reversion (forex not in
    MR's asset_classes) and, with macro/trend opposing but no aligned
    momentum read, falls through to stand_by — momentum requires_aligned."""
    r = route(
        symbol="EURUSD", signal="buy",
        regime_label="low_vol_bull", asset_class="forex",
        macro_label="bear", trend_label="trending_down",
    )
    assert r.archetype != "mean_reversion"      # forex excluded from MR
    # momentum requires_aligned and this is a counter-trend read -> no match.
    assert r.matched is False
    assert r.archetype == "stand_by"


def test_neutral_signal_never_routes_directional():
    r = route(
        symbol="XAUUSD", signal="neutral",
        regime_label="low_vol_bull", asset_class="metal",
        macro_label="bear", trend_label="trending_down",
    )
    assert r.matched is False
    assert r.archetype == "stand_by"
    assert "no directional" in r.reason


def test_mean_reversion_respects_regime_vol_gate():
    """MR is regime_vol-gated to low/med_low. A gold buy into downtrend in a
    HIGH-vol regime must NOT route to MR (exhaustion reversion is a calm-tape
    play; high-vol counter-trend is the falling-knife case)."""
    r = route(
        symbol="XAUUSD", signal="buy",
        regime_label="high_vol_bull", asset_class="metal",
        macro_label="bear", trend_label="trending_down",
    )
    assert r.archetype != "mean_reversion"


def test_stand_by_when_no_reads_available():
    """A gold buy with no macro/trend reads: MR needs requires_opposes=True
    but opposes is None -> no match; momentum needs aligned -> no match ->
    stand_by (the safe no-trade default)."""
    r = route(
        symbol="XAUUSD", signal="buy",
        regime_label="low_vol_bull", asset_class="metal",
        macro_label=None, trend_label=None,
    )
    assert r.matched is False
    assert r.archetype == "stand_by"


def test_router_never_raises_on_bad_input():
    """Fail-open: garbage inputs return stand_by, never raise into the sweep."""
    r = route(
        symbol="XAUUSD", signal="buy",
        regime_label=None, asset_class=None,
        macro_label=None, trend_label=None,
    )
    assert isinstance(r, RoutedStrategy)
    assert r.archetype in ("stand_by", "momentum", "mean_reversion")


def test_routing_is_deterministic():
    """Same inputs -> same verdict (R2 measurement assumes determinism)."""
    kwargs = dict(
        symbol="XAUUSD", signal="buy", regime_label="med_low_vol_bull",
        asset_class="metal", macro_label="bear", trend_label="reversal_down",
    )
    a = route(**kwargs)
    b = route(**kwargs)
    assert a.archetype == b.archetype
    assert a.as_stamp() == b.as_stamp()


def test_orchestrator_r4_block_dependencies_are_module_importable():
    """Regression: the R4 clef-archetype block in _process_symbol calls
    get_clef_flash_mode() at module scope, but only get_router_mode was added
    to the top-level feature_flags import — so the block raised NameError,
    was swallowed by its except, and silently never stamped (R4=0 for a day).
    Pin that every name the R4 block references resolves at import time."""
    import regime_platform.sweep_orchestrator.orchestrator as orch
    for name in ("get_router_mode", "get_clef_flash_mode"):
        assert hasattr(orch, name), (
            f"orchestrator must import {name} at module scope — the R4 block "
            f"references it directly"
        )
    # The R4/R5 classifier entry points must be importable too.
    from regime_platform.services.decision.clef_flash import classify_archetype
    from regime_platform.sweep_orchestrator.clef_stance import classify_stance
    assert callable(classify_archetype) and callable(classify_stance)
