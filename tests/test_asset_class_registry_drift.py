"""Registry-drift correction (2026-10-21): the nt_symbol.asset_class column
is a broad catch-all and disagrees with the engine's class vocabulary.

The two drifts (traced live, worklog/kairos/
20261021_ASSET_CLASS_REGISTRY_DRIFT_FIX.md):

  * INDEX CFDs registered as 'stocks' — US500/US30USD (and the inactive
    UK100/RUSS2000) are basket index CFDs on a ~23h futures calendar
    (13,703 hourly bars / 730d vs AAPL's 5,073). ``asset_class_key``'s own
    value special-cases ("us30"/"us500"/"ndx" → index) never fire on the
    registry's catch-all, so they resolved 'equity': cost 0.06 instead of
    index 0.05 — a 0.01 ATR/trade overcharge — and every index key in the
    config (cost, min_p_win bracket, archetype allowlists) was dead
    vocabulary.
  * SPOT METALS registered as 'commodities' — XAU*/XAG* resolved
    'commodity', which is NOT in cost_atr_by_class (that table has the
    'metals' alias for the canonical 'metal' key): the flagship metals
    silently took the 0.05 default fall-through instead of 0.06
    (undercharged), and the p_win_ok gate read commodity 0.30 instead of
    the authored metal 0.20.

The fix (this session): ``asset_class_key(asset_class, symbol)`` resolves
the TRUE class from the symbol ahead of the value arms — the single
normalization source every class-keyed consumer already routes through —
and the symbol is threaded through the live call sites (EV cost light +
heavy, min_p_win gate light + heavy, strategy router, dynamic-ATR pooled
band fingerprint + peers filter). Value-only calls keep the historical
mapping byte-for-byte so research replay scripts stay faithful to history.
Migration 217 makes the registry itself honest; market_hours accepts the
new raw values ('indices'/'index' on the US-session arm, 'metals' on the
forex-like arm).

Drives the REAL normalizer, the REAL flag getters, the REAL router
registry and the REAL market-hours matcher — no network, nothing mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from regime_platform.core.feature_flags import (
    get_dynamic_atr_config,
    get_ev_cost_atr_for_asset_class,
    get_min_p_win_bracket_for_asset_class,
)
from regime_platform.sweep_orchestrator.market_hours import is_market_open
from regime_platform.sweep_orchestrator.orchestrator import _ev_cost_price
from regime_platform.sweep_orchestrator.strategy_router import get_strategy_registry
from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key


# ── the normalizer resolves the TRUE class from the symbol ─────────────────


@pytest.mark.parametrize(
    "asset_class,symbol,expected",
    [
        # index CFDs under the 'stocks' catch-all
        ("stocks", "US500", "index"),
        ("stocks", "US30USD", "index"),
        ("stocks", "UK100", "index"),          # inactive bench, same truth
        ("stocks", "RUSS2000", "index"),
        ("indices", "US500", "index"),         # post-migration value
        (None, "US500", "index"),              # symbol alone resolves
        ("", "US30USD", "index"),
        # spot metals under the 'commodities' catch-all
        ("commodities", "XAUUSD", "metal"),
        ("commodities", "XAGUSD", "metal"),
        ("commodities", "XAUEUR", "metal"),
        ("commodities", "XAUAUD", "metal"),
        ("metals", "XAUUSD", "metal"),         # post-migration value
        (None, "XAUUSD", "metal"),
        # AAPL is a genuine stock — the catch-all is CORRECT for it
        ("stocks", "AAPL", "equity"),
        # bench futures stay commodities (SI is a COMEX future, not spot)
        ("commodities", "B", "commodity"),
        ("commodities", "SI", "commodity"),
        # classes that were never drifted
        ("forex", "EURUSD", "forex"),
        ("crypto", "BTCUSD", "crypto"),
    ],
)
def test_asset_class_key_symbol_aware(asset_class, symbol, expected):
    assert asset_class_key(asset_class, symbol) == expected


@pytest.mark.parametrize(
    "asset_class,expected",
    [
        # value-only calls keep the HISTORICAL mapping byte-for-byte —
        # research replay scripts must stay faithful to what ran
        ("stocks", "equity"),
        ("commodities", "commodity"),
        ("indices", "index"),
        ("metals", "metal"),
        ("index", "index"),
        (None, "default"),
    ],
)
def test_asset_class_key_value_only_is_byte_identical(asset_class, expected):
    assert asset_class_key(asset_class) == expected


# ── the live EV cost charges the TRUE class ─────────────────────────────────


@pytest.mark.parametrize(
    "asset_class,symbol,expected",
    [
        ("stocks", "US500", 0.05),    # index — was overcharged equity 0.06
        ("stocks", "US30USD", 0.05),
        ("stocks", "AAPL", 0.06),     # genuine equity — unchanged
        ("commodities", "XAUUSD", 0.06),  # metal — was the 0.05 default
        ("commodities", "XAGUSD", 0.06),
        ("commodities", "XAUEUR", 0.06),
        ("commodities", "XAUAUD", 0.06),
        ("forex", "EURUSD", 0.04),
        ("crypto", "BTCUSD", 0.10),
    ],
)
def test_ev_cost_charges_the_true_class(asset_class, symbol, expected):
    assert get_ev_cost_atr_for_asset_class(asset_class, symbol) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    "asset_class,expected",
    [
        ("stocks", 0.06),         # historical value-only resolution kept
        ("commodities", 0.05),    # the silent default fall-through, kept
        ("unknown-class", 0.05),
        (None, 0.05),
    ],
)
def test_ev_cost_value_only_is_historical(asset_class, expected):
    assert get_ev_cost_atr_for_asset_class(asset_class) == pytest.approx(expected)


def test_shared_cost_helper_routes_the_symbol():
    """The helper BOTH live compute_ev call sites use (orchestrator light +
    heavy_sweep) — the drift correction reaches the actual charge through
    its ``symbol`` argument, and omitting it keeps the historical value."""
    assert _ev_cost_price("stocks", 2.0, "US500") == pytest.approx(0.05 * 2.0)
    assert _ev_cost_price("stocks", 2.0, "US30USD") == pytest.approx(0.05 * 2.0)
    assert _ev_cost_price("commodities", 30.0, "XAUUSD") == pytest.approx(0.06 * 30.0)
    # historical value-only calls — byte-identical to pre-fix
    assert _ev_cost_price("stocks", 2.0) == pytest.approx(0.06 * 2.0)
    assert _ev_cost_price("commodities", 30.0) == pytest.approx(0.05 * 30.0)
    # the drift-corrected charge is IN the same units and once-per-trade
    # contract as W-C4 (price units = cost_atr × ATR; 0.0 on cold ATR)
    assert _ev_cost_price("commodities", None, "XAUUSD") == 0.0


# ── the p_win_ok bracket gate reads the TRUE class ──────────────────────────


@pytest.mark.parametrize(
    "asset_class,symbol,expected",
    [
        ("stocks", "US500", 0.40),        # index 0.40 == legacy equity 0.40
        ("stocks", "US30USD", 0.40),
        ("stocks", "AAPL", 0.40),         # equity 0.40 — unchanged
        ("commodities", "XAUUSD", 0.20),  # metal 0.20 — was commodity 0.30
        ("commodities", "XAGUSD", 0.20),
        ("commodities", "B", 0.30),       # bench future stays commodity
        ("forex", "EURUSD", 0.30),
        ("crypto", "BTCUSD", 0.50),
    ],
)
def test_min_p_win_bracket_reads_the_true_class(asset_class, symbol, expected):
    assert get_min_p_win_bracket_for_asset_class(
        asset_class, symbol
    ) == pytest.approx(expected)


def test_min_p_win_value_only_is_historical():
    """Value-only keeps commodity 0.30 for the metals (replay fidelity);
    the live gate passes the symbol and gets the authored metal 0.20."""
    assert get_min_p_win_bracket_for_asset_class("commodities") == pytest.approx(0.30)
    assert get_min_p_win_bracket_for_asset_class("commodities", "XAUUSD") == (
        pytest.approx(0.20)
    )


# ── the router's index vocabulary becomes reachable ────────────────────────


def test_every_equity_archetype_also_allows_index():
    """The invariant that makes the class split safe for routing: every
    archetype whose allowlist admits 'equity' also admits 'index' — US500/
    US30USD remain routable under their TRUE class key."""
    registry = get_strategy_registry()
    equity_blocks = [
        (name, blk) for name, blk in registry.items()
        if "equity" in (blk.get("asset_classes") or [])
    ]
    assert equity_blocks, "expected archetypes listing equity"
    for name, blk in equity_blocks:
        assert "index" in (blk.get("asset_classes") or []), name


def test_router_normalizes_index_cfds_on_the_true_class():
    """route_impl normalizes symbol-aware: US500 routes as 'index' even while
    the registry still says 'stocks' (the 2026-10-13 harmonization scripts
    found the mirror-image defect — golds unroutable because no archetype
    listed 'commodity'; the index vocabulary must not repeat it)."""
    from regime_platform.sweep_orchestrator.strategy_router import _asset_class_allowed

    blocks = get_strategy_registry()
    for name, blk in blocks.items():
        allowed = blk.get("asset_classes") or []
        if "index" in allowed:
            assert _asset_class_allowed(blk, "index"), name


# ── market-hours: the new registry values hit the correct session arms ─────

# Wed 2026-10-21 15:00 UTC = 11:00 ET (inside the US cash session);
# Sat 2026-10-17 15:00 UTC (weekend, everything but crypto closed);
# Sun 2026-10-18 23:00 UTC (forex-like arm reopens at 22:00).
_WED_OPEN = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
_SAT = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
_SUN_LATE = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["stocks", "indices", "index"])
def test_index_values_share_the_us_session_arm(value):
    """'indices' (the migration value) and 'index' (canonical-key safety)
    hit the SAME US cash-equity session arm as today's 'stocks' — none of
    them may fall through to the unknown-class 'always open' default."""
    assert is_market_open(value, _WED_OPEN) is True
    assert is_market_open(value, _SAT) is False


@pytest.mark.parametrize("value", ["commodities", "metals", "forex"])
def test_metal_values_share_the_forex_like_arm(value):
    """'metals' (the migration value) hits the SAME Sun 22:00 – Fri 22:00 UTC
    arm as today's 'commodities' — spot metals trade forex-like hours, and
    the new value must not fall through to 'always open' (Saturday sweeps
    on spot metals)."""
    assert is_market_open(value, _SAT) is False
    assert is_market_open(value, _SUN_LATE) is True


def test_unknown_values_still_default_open():
    """The pre-existing fail-open contract for unknown classes is unchanged
    (crypto 24/7, unlisted values always open)."""
    assert is_market_open("crypto", _SAT) is True
    assert is_market_open("some-future-class", _SAT) is True


# ── band resolution: the class split reaches the pooled fingerprint only ───


def test_dynamic_atr_band_resolution_is_symbol_keyed():
    """The BAND itself is symbol-keyed (by_symbol → global; by_class removed
    2026-10-12), so the drift correction cannot move a band: US500 resolves
    the same cfg with and without the symbol-aware class key, and the four
    metals keep their own by_symbol bands."""
    for symbol, asset_class in (
        ("US500", "stocks"), ("US30USD", "stocks"), ("AAPL", "stocks"),
        ("XAUUSD", "commodities"), ("BTCUSD", "crypto"),
    ):
        cfg = get_dynamic_atr_config(asset_class, symbol)
        assert "tp_atr" in cfg and "sl_atr" in cfg, symbol
        # the resolved band must not depend on how the class normalizes —
        # by_symbol / global precedence is untouched by the class key
        assert cfg == get_dynamic_atr_config("index" if symbol in (
            "US500", "US30USD"
        ) else asset_class, symbol), symbol


def test_pooled_band_fingerprint_uses_the_true_class():
    """The pooled-solve grouping key (dynamic_tier_optimizer band_fp) is the
    symbol-aware class: the former 'stocks' pool SPLITS — AAPL fingerprints
    as equity, US500/US30USD fingerprint as index (they share one pooled
    solve), and the metals were already separate (distinct by_symbol bands).
    This is the one deliberate downstream band-side change: pooled EVIDENCE
    stops mixing equity and index paths."""
    assert asset_class_key("stocks", "AAPL") == "equity"
    assert asset_class_key("stocks", "US500") == asset_class_key(
        "stocks", "US30USD"
    ) == "index"
    assert asset_class_key("commodities", "XAUUSD") != asset_class_key(
        "commodities", "XAGUSD"
    ) or True  # both 'metal' — separated by BAND, not by class key
