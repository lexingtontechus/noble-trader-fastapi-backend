"""E2E + replay: reachability-conditioned probability (the calibration reframe).

Offline + deterministic. Encodes the era-clean reachability cells from the
2026-10-11 pulse (nt_bracket_reachability, signal='all', atr_source=stored):
tight buckets stop out ~91-100%, only the 4+ bucket reaches ~19% TP (30%
expire); XAUUSD buy trending_down 38.2% vs consolidating 8.1%.

Verifies:
  * the estimator reads a real cell and reproduces its era-clean TP rate at a
    covering leash,
  * expiry is a monotonic haircut (longer leash never lowers P; a short leash
    cuts the tail),
  * a tight bracket prices ~0 regardless of direction,
  * direction edge modulates within a reachable cell WITHOUT letting a
    direction-only signal re-introduce overconfidence,
  * fail-open on cold/missing cells (None, never a fabricated number),
  * compare_to_blend flags the direction-only blend as OVERPRICED vs
    reachability — the reframe's core diagnostic.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.reachability_prob import (
    MIN_CELL_N,
    compare_to_blend,
    reachability_conditioned_probability,
)


# Era-clean reachability cells (2026-10-11 pulse, signal='all', rounded).
# distance_bucket -> (n, pct_hit_tp, pct_hit_sl, pct_expired, median_h, p90_h)
ERA_CELLS = [
    # XAUUSD wide bucket — the only one that reaches TP; 30% expire, p90 56h.
    {"symbol": "XAUUSD", "vol_tier": "med_low_vol", "distance_atr_bucket": "4+",
     "signal": "all", "n": 214, "pct_hit_tp": 0.192, "pct_hit_sl": 0.511,
     "pct_expired": 0.297, "median_hours_to_resolve": 41.0, "p90_hours": 56.0},
    # XAUUSD tight bucket — stops out ~91%, never reaches, resolves <1h.
    {"symbol": "XAUUSD", "vol_tier": "med_low_vol", "distance_atr_bucket": "1-2",
     "signal": "all", "n": 180, "pct_hit_tp": 0.090, "pct_hit_sl": 0.910,
     "pct_expired": 0.0, "median_hours_to_resolve": 0.5, "p90_hours": 1.0},
    # XAUUSD buy trending_down cohort — the MR edge cell (13/34 = 0.382).
    {"symbol": "XAUUSD", "vol_tier": "med_low_vol", "distance_atr_bucket": "4+",
     "signal": "buy", "n": 34, "pct_hit_tp": 0.382, "pct_hit_sl": 0.618,
     "pct_expired": 0.0, "median_hours_to_resolve": 30.0, "p90_hours": 48.0},
    # XAUUSD buy consolidating cohort — no edge (11/136 = 0.081).
    {"symbol": "XAUUSD", "vol_tier": "low_vol", "distance_atr_bucket": "4+",
     "signal": "buy", "n": 136, "pct_hit_tp": 0.081, "pct_hit_sl": 0.919,
     "pct_expired": 0.0, "median_hours_to_resolve": 44.0, "p90_hours": 60.0},
    # A cold cell (n below floor) — must fail open.
    {"symbol": "XAUEUR", "vol_tier": "low_vol", "distance_atr_bucket": "4+",
     "signal": "all", "n": 8, "pct_hit_tp": 0.30, "pct_hit_sl": 0.70,
     "pct_expired": 0.0, "median_hours_to_resolve": 20.0, "p90_hours": 40.0},
]


# ── Base rate reproduces the era-clean cell ─────────────────────────────────

def test_wide_bucket_reproduces_era_clean_rate_at_covering_leash():
    """At a leash >= the cell's p90 (56h), P equals the era-clean TP share."""
    r = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="4+", expiry_hours=56.0, signal="all")
    assert r is not None and r.matched is True
    assert abs(r.p_reach - 0.192) < 0.01
    assert r.cell_n == 214
    assert r.expiry_factor == 1.0


def test_tight_bucket_prices_near_zero():
    """A <2 ATR bracket stops out ~91% and rarely reaches TP — the estimator
    must reflect that geometry, not the direction."""
    r = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="1-2", expiry_hours=48.0, signal="all")
    assert r is not None
    assert r.p_reach <= 0.10, "tight bracket must price ~ its 9% TP share"


# ── Expiry is a monotonic haircut ───────────────────────────────────────────

def test_expiry_is_monotonic_and_cuts_tail():
    """Longer leash never lowers P; a leash shorter than p90 cuts the tail."""
    short = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="4+", expiry_hours=28.0, signal="all")  # half of p90=56
    full = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="4+", expiry_hours=112.0, signal="all")
    assert short is not None and full is not None
    assert abs(short.expiry_factor - 0.5) < 0.01     # 28/56
    assert full.expiry_factor == 1.0                  # capped
    assert full.p_reach > short.p_reach
    assert abs(short.p_reach - round(0.192 * 0.5, 4)) < 0.01


# ── Direction edge modulates within a reachable cell ────────────────────────

def test_direction_edge_preserves_trend_gap_within_cell():
    """The trending_down buy cell (0.382) prices above the consolidating buy
    cell (0.081) — the direction/regime edge survives conditioning, but both
    stay anchored to their reachability base rate (no direction-only spike).
    Compared at each cell's covering leash (expiry >= its p90) so P equals the
    era-clean WR for both."""
    down = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="4+", expiry_hours=48.0, signal="buy")   # p90 48
    cons = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="low_vol",
        distance_bucket="4+", expiry_hours=60.0, signal="buy")   # p90 60
    assert down is not None and cons is not None
    assert abs(down.p_reach - 0.382) < 0.01
    assert abs(cons.p_reach - 0.081) < 0.01
    assert down.p_reach / cons.p_reach > 4.0


def test_consolidating_cell_haircut_by_short_leash():
    """The same consolidating cell under a 48h leash (below its 60h p90) is
    haircut to 0.8x — expiry binds independently of direction."""
    cons_short = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="low_vol",
        distance_bucket="4+", expiry_hours=48.0, signal="buy")
    assert cons_short is not None
    assert abs(cons_short.expiry_factor - 0.8) < 0.01
    assert abs(cons_short.p_reach - round(0.081 * 0.8, 4)) < 0.01


def test_direction_edge_modulates_but_cannot_dominate():
    """A within-cell direction edge nudges P in logit space; even a strong
    edge cannot push a 9% tight-bracket cell to a momentum-like 50% — geometry
    still binds (the whole point of the reframe)."""
    base = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="1-2", expiry_hours=48.0, signal="all")
    edged = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="1-2", expiry_hours=48.0, signal="all",
        direction_edge=0.5)  # strong positive direction edge
    assert base is not None and edged is not None
    assert edged.p_reach > base.p_reach           # edge pushes up
    assert edged.p_reach < 0.20, "geometry must still bind a tight bracket"


# ── Fail-open ───────────────────────────────────────────────────────────────

def test_cold_cell_fails_open_not_matched():
    """n below the floor -> matched=False (caller keeps its existing p_win)."""
    r = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUEUR", vol_tier="low_vol",
        distance_bucket="4+", expiry_hours=48.0, signal="all")
    assert r is not None
    assert r.matched is False
    assert r.cell_n < MIN_CELL_N


def test_missing_cell_returns_none():
    """No matching (symbol, vol_tier, bucket) -> None, never a fabricated P."""
    assert reachability_conditioned_probability(
        ERA_CELLS, symbol="EURUSD", vol_tier="low_vol",
        distance_bucket="4+", expiry_hours=48.0) is None
    assert reachability_conditioned_probability(
        [], symbol="XAUUSD", vol_tier="low_vol",
        distance_bucket="4+", expiry_hours=48.0) is None


# ── The reframe's core diagnostic ───────────────────────────────────────────

def test_compare_to_blend_flags_overpricing():
    """A direction-only blend of 0.36 (the era-clean avg_p_win) vs a wide-cell
    reachability read of 0.19 must flag OVERPRICED — the overconfidence the
    reframe removes, quantified per signal."""
    r = reachability_conditioned_probability(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol",
        distance_bucket="4+", expiry_hours=56.0, signal="all")
    cmp = compare_to_blend(r.p_reach if r else None, p_win_blend=0.36)
    assert cmp["delta"] is not None
    assert cmp["delta"] > 0.15, "blend must read as overpriced vs reachability"
    assert cmp["direction"] == "overpriced"


def test_compare_to_blend_no_reach_read():
    assert compare_to_blend(None, p_win_blend=0.36)["delta"] is None
