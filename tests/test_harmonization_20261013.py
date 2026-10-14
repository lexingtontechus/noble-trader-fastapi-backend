"""HARM-01/02/03/04 — harmonization regression tests.

Four defects found 2026-10-13 (pulse check + router replay):

  HARM-01  Asset-class vocabulary split. ``nt_symbol.asset_class`` for the
           four golds is ``commodities`` -> ``asset_class_key()`` ->
           ``'commodity'``, but the registry gates ``mean_reversion`` to
           ``asset_classes: [metal]`` and ``momentum`` to
           ``[crypto, metal, equity, index, etf]``. Neither list contains
           ``commodity``, so a gold row can never match a gold archetype.
           Replay of 225 last-12h resolved rows: routed 0/225.

  HARM-02  Deadline floors declared but never enforced. ``feature_flags.yaml``
           declares ``min_soft_deadline_hours: 12.0`` /
           ``max_hard_deadline_hours: 120.0``, and
           ``get_dynamic_deadline_config_for_symbol()`` returns them, but the
           derivation path (``dynamic_tier_optimizer._compute_dynamic_deadline``)
           writes raw p75/p90 with no clamp. Live consequence: AUDUSD stamped
           ``soft_deadline_hours = 0.5`` against a 2.62h average settle; 118 of
           225 resolved rows breach their own soft deadline.

  HARM-03  Reachability cadence is not maintained. The table is built by a
           weekly Sunday 07:00 UTC cron plus the nightly EOD pass, but the
           delete-then-upsert leaves exactly one ``computed_at`` in the table,
           so the only observable freshness signal is a single timestamp with
           no age guard. ``BracketReachabilityStaleAfter`` is 6.5 days while
           the weekly job is 7 days apart — the guard can never fire before
           the next scheduled build.

  HARM-04  Router inputs (macro_label / trend_label) are a single daily
           refresh (06:00 / 06:10 UTC) while signals are emitted every 15
           minutes, so the tri-state alignment the router depends on is up to
           24h stale by construction. Measured: zero rows in 3 days with both
           reads directional, last such row 2026-10-09 06:00Z — i.e. the
           alignment is frozen at 'consolidating' for whole days.
"""

from __future__ import annotations

import pytest

from regime_platform.core import feature_flags as ff
from regime_platform.sweep_orchestrator.strategy_router import route
from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key

# ── HARM-01 ───────────────────────────────────────────────────────────────

# The real nt_symbol.asset_class values for the active book. The golds are
# 'commodities', NOT 'metal' — this is the load-bearing fact.
ACTIVE_ASSET_CLASSES = {
    "AAPL": "stocks",
    "US30USD": "stocks",
    "US500": "stocks",
    "AUDUSD": "forex",
    "EURUSD": "forex",
    "GBPUSD": "forex",
    "USDCAD": "forex",
    "USDCHF": "forex",
    "USDJPY": "forex",
    "BTCUSD": "crypto",
    "XAUUSD": "commodities",
    "XAUAUD": "commodities",
    "XAUEUR": "commodities",
    "XAGUSD": "commodities",
}

# Symbols whose asset_class is deliberately not served by any enabled
# archetype, with the reason. The assertion above allows exactly this set —
# adding a symbol here is a strategy decision and must carry its evidence.
INTENTIONALLY_UNCOVERED = {
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
}


def test_asset_class_key_of_every_active_symbol_is_reachable_by_some_enabled_archetype():
    """HARM-01: for every active symbol, the asset_class_key its own
    nt_symbol.asset_class produces must appear in at least one ENABLED
    archetype's asset_classes list. Otherwise the symbol can never route to
    anything but stand_by, no matter what its macro/trend reads say.

    This is the single-source-of-truth assertion: the registry's vocabulary
    and the key vocabulary must be the same set.
    """
    reg = ff.get_strategy_registry()
    enabled_keys: set[str] = set()
    for blk in reg.values():
        if not isinstance(blk, dict) or not blk.get("enabled"):
            continue
        for k in blk.get("asset_classes") or ():
            enabled_keys.add(str(k))

    unreachable = {}
    for symbol, asset_class in ACTIVE_ASSET_CLASSES.items():
        key = asset_class_key(asset_class)
        if key not in enabled_keys:
            unreachable[symbol] = (asset_class, key)

    # Forex is INTENTIONALLY uncovered: the B1 walk-forward (2026-10-12/13)
    # measured 0 false-cohort rows across ~3,300 era-clean resolved forex
    # signals — the engine only emits forex signals aligned with the
    # regime/trend read, so there is no FX mean-reversion cohort to serve, and
    # ma_trend (the FX-aligned archetype) stays disabled pending its own
    # era-clean evidence separating it from momentum. Recorded, not ignored.
    assert set(unreachable) <= set(INTENTIONALLY_UNCOVERED), (
        "these symbols' asset_class_key appears in no enabled archetype's "
        f"asset_classes — they can only ever route stand_by: {unreachable}"
    )


def test_metals_are_reachable_after_the_harmonization_fix():
    """The HARM-01 regression: before 2026-10-13 all four golds were
    unreachable because 'commodity' was in no enabled archetype's list, so
    the router replayed 0/225. Metals must now be covered."""
    reg = ff.get_strategy_registry()
    enabled_keys: set[str] = set()
    for blk in reg.values():
        if not isinstance(blk, dict) or not blk.get("enabled"):
            continue
        for k in blk.get("asset_classes") or ():
            enabled_keys.add(str(k))

    for symbol in ("XAUUSD", "XAUAUD", "XAUEUR", "XAGUSD"):
        key = asset_class_key(ACTIVE_ASSET_CLASSES[symbol])
        assert key in enabled_keys, (
            f"{symbol} ({ACTIVE_ASSET_CLASSES[symbol]} -> {key}) is unreachable "
            "again — the HARM-01 asset-class regression is back"
        )


def test_gold_buy_opposing_macro_and_trend_routes_mean_reversion():
    """HARM-01 behavioural test: a XAUUSD buy with macro=bear and
    trend=trending_down is exactly the ID45 pattern the mean_reversion
    archetype was written for, using XAUUSD's REAL nt_symbol.asset_class.
    Before the fix this routed stand_by because 'commodity' ∉ [metal].

    regime_label is low_vol_bull so the archetype's own regime_vol allow-list
    is satisfied — this test isolates the asset-class defect only. The
    regime_vol/evidence contradiction is pinned separately below.
    """
    routed = route(
        symbol="XAUUSD",
        signal="buy",
        regime_label="low_vol_bull",
        asset_class="commodities",  # the real nt_symbol value
        macro_label="bear",
        trend_label="trending_down",
    )
    assert routed.archetype == "mean_reversion", (
        f"XAUUSD buy opposing both reads must route mean_reversion, got {routed.archetype!r} ({routed.reason})"
    )


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAUAUD", "XAUEUR", "XAGUSD"])
def test_every_gold_symbol_can_reach_mean_reversion(symbol):
    routed = route(
        symbol=symbol,
        signal="buy",
        regime_label="low_vol_bull",
        asset_class="commodities",
        macro_label="bear",
        trend_label="trending_down",
    )
    assert routed.archetype == "mean_reversion", (
        f"{symbol} buy opposing both reads must route mean_reversion, got {routed.archetype!r} ({routed.reason})"
    )


def test_metal_key_still_accepted_for_backwards_compatibility():
    """A symbol whose asset_class literally reads 'metal' must keep routing —
    the fix widens the vocabulary, it does not replace it."""
    routed = route(
        symbol="XAUUSD",
        signal="buy",
        regime_label="low_vol_bull",
        asset_class="metal",
        macro_label="bear",
        trend_label="trending_down",
    )
    assert routed.archetype == "mean_reversion"


def test_regime_vol_gate_excludes_the_measured_best_evidence_cell():
    """HARM-01(c) — DOCUMENTED, NOT FIXED.

    The era-clean false-cohort WR by vol tier (2026-10-13 measurement) is
    med_high_vol 52.9% (n=17) > low_vol 27.3% (n=11) > high_vol 16.7% (n=6),
    but the registry gates ``regime_vol: [low_vol, med_low_vol]`` — excluding
    the best cell. This test PINS the contradiction so it cannot be widened
    silently: changing the allow-list is a strategy decision that changes
    which signals route live, and it needs the same evidence bar as the
    B1 cohort flip. Asserted, not asserted-away.
    """
    from regime_platform.sweep_orchestrator.strategy_router import _vol_tier

    mr = ff.get_strategy_archetype("mean_reversion")
    allowed = set(mr.get("regime_vol") or ())

    measured = {"med_high_vol": 0.529, "low_vol": 0.273, "high_vol": 0.167}
    best_tier = max(measured, key=measured.get)

    assert _vol_tier("med_high_vol_strong_bull") == "med_high_vol"
    assert best_tier not in allowed, (
        f"the measured best vol tier ({best_tier}, WR {measured[best_tier]:.1%}) "
        f"is now inside the registry's regime_vol allow-list {sorted(allowed)}. "
        "Widening it is a live-routing strategy change — it must go through the "
        "B1 evidence bar, not a plumbing fix. If this was deliberate, update "
        "tests/test_harmonization_20261013.py and nt_roadmap together."
    )


# ── HARM-02 ───────────────────────────────────────────────────────────────


def test_dynamic_deadline_derivation_respects_the_declared_floors():
    """HARM-02: the deadline the optimizer derives must be clamped to the
    floors feature_flags.yaml declares. A 0.5h soft deadline is unusable —
    the sweep's own resolve takes 2.6h on that symbol.
    """
    from regime_platform.sweep_orchestrator.dynamic_tier_optimizer import (
        _compute_dynamic_deadline,
    )

    cfg = ff.get_dynamic_deadline_config()
    min_soft = float(cfg.get("min_soft_deadline_hours", 12.0))
    max_hard = float(cfg.get("max_hard_deadline_hours", 120.0))

    # A reachability row whose p75/p90 are far below the floor — the AUDUSD
    # shape that shipped 0.5h.
    reach_rows = [
        {
            "symbol": "AUDUSD",
            "vol_tier": "high_vol",
            "signal": "all",
            "n": 200,
            "median_hours_to_resolve": 0.3,
            "p75_hours": 0.5,
            "p90_hours": 0.6,
        }
    ]

    tiers = _compute_dynamic_deadline(reach_rows)
    assert "AUDUSD:high_vol" in tiers, f"no tier computed: {list(tiers)}"
    tier = tiers["AUDUSD:high_vol"]

    assert tier["soft_deadline_hours"] >= min_soft, (
        f"soft deadline {tier['soft_deadline_hours']}h is below the declared "
        f"floor {min_soft}h — feature_flags.yaml min_soft_deadline_hours is "
        "declared but not enforced"
    )
    assert tier["hard_deadline_hours"] <= max_hard, (
        f"hard deadline {tier['hard_deadline_hours']}h exceeds the declared ceiling {max_hard}h"
    )
    assert tier["hard_deadline_hours"] > tier["soft_deadline_hours"], (
        "hard deadline must exceed soft deadline or decay() divides by a non-positive span"
    )


def test_dynamic_deadline_derivation_clamps_an_absurdly_wide_tier():
    """The same clamp must bound the upper tail: a p90 of 400h is not a
    deadline, it is 'never'."""
    from regime_platform.sweep_orchestrator.dynamic_tier_optimizer import (
        _compute_dynamic_deadline,
    )

    cfg = ff.get_dynamic_deadline_config()
    max_hard = float(cfg.get("max_hard_deadline_hours", 120.0))

    reach_rows = [
        {
            "symbol": "XAUUSD",
            "vol_tier": "low_vol",
            "signal": "all",
            "n": 200,
            "median_hours_to_resolve": 200.0,
            "p75_hours": 300.0,
            "p90_hours": 400.0,
        }
    ]
    tier = _compute_dynamic_deadline(reach_rows)["XAUUSD:low_vol"]
    assert tier["hard_deadline_hours"] <= max_hard


# ── HARM-03 ───────────────────────────────────────────────────────────────


def test_reachability_staleness_guard_fires_before_the_next_build():
    """HARM-03: the startup staleness threshold must be strictly SHORTER than
    the scheduled build interval, otherwise the guard can never fire before
    the next scheduled build and 'stale' is unobservable.
    """
    from regime_platform.sweep_orchestrator import orchestrator as orch

    stale_after = orch.BRACKET_REACHABILITY_STALE_AFTER
    weekly_interval_h = 7 * 24  # the '0 7 * * sun' cron

    assert stale_after.total_seconds() / 3600.0 < weekly_interval_h, (
        f"BRACKET_REACHABILITY_STALE_AFTER ({stale_after}) is not shorter than "
        f"the {weekly_interval_h}h weekly build interval — the staleness guard "
        "can never fire before the next scheduled build"
    )


# ── HARM-04 ───────────────────────────────────────────────────────────────

def test_router_input_refresh_cadence_is_at_least_as_fast_as_the_signal_cadence():
    """HARM-04: the router's alignment inputs (macro_label, trend_label) must
    refresh on the pre-sweep grid (5,20,35,50 * * * *) so labels are at
    most ~5 min stale when signals fire — not 24h stale.

    This pins that the refresh cadence is REGISTERED (not merely configured)
    so a schedule fix cannot silently drop the job.
    """
    import inspect

    from regime_platform.sweep_orchestrator import orchestrator as orch

    src = inspect.getsource(orch)
    # Both label refreshers must be registered on the pre-sweep grid.
    assert "macro-regime-refresh" in src, (
        "macro regime refresh job is not registered on the pre-sweep grid"
    )
    assert "trend-regime-refresh" in src, (
        "trend regime refresh job is not registered on the pre-sweep grid"
    )
    # Job schedules must match the pre-sweep grid: 5,20,35,50 * * * *
    assert 'from_crontab("5,20,35,50 * * * *")' in src, (
        "macro/trend regime jobs not scheduled on pre-sweep grid (5,20,35,50 * * * *)"
    )
    # Job ids must be the new names (not the old daily names)
    assert "macro-regime-daily" not in src, (
        "old 'macro-regime-daily' job id still present — schedule change incomplete"
    )
    assert "trend-regime-daily" not in src, (
        "old 'trend-regime-daily' job id still present — schedule change incomplete"
    )
