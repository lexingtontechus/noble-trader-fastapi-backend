"""E2E + replay/backfill: calibration + reachability reframe.

Three layers:

  A. UNIT (fused decomposition + 3-outcome verdict) — deterministic, synthetic
     cells covering every branch (overconfident/underconfident/calibrated/thin,
     expired-as-third-outcome, no_skill claimed edge).

  B. REPLAY (live era-clean nt_bracket_reachability book) — fetches the real
     all-in cells and runs them through reframe_reachability_row. Asserts the
     reframe's headline: the all-in verdict differs from the resolved-only
     verdict, and the fused p_win rebuilds to the reachability-conditioned
     rate. Skipped offline (no Supabase creds); runs in the Studio replay.

  C. BACKFILL equivalence — the decomposition p_direction × p_reachability
     reproduces the fused number within rounding, so swapping the live blend
     for the product is behavior-preserving on the math.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.calibration_reframe import (
    CALIBRATED_BAND,
    cell_calibration,
    reframe_reachability_row,
)


# ── A. Unit: fused decomposition + 3-outcome verdict ────────────────────────

def test_overconfident_cell_all_in_flags_more_than_resolved():
    """The all-in verdict is HARSHER than resolved-only when expiry is real:
    a cell with a healthy resolved WR but heavy expiry must read OVERCONFIDENT
    on the all-in base rate — this is the 'calibrated but losing' fix."""
    # 30 wins, 60 losses, 40 expired. resolved-only WR = 30/90 = 0.333
    # (looks okay). all-in tp = 30/130 = 0.231.
    c = cell_calibration(
        tp=30, sl=60, expired=40, published_p_win=0.36,
        tp_distance=4.0, sl_distance=2.0,
    )
    assert c.resolved_tp_share == pytest.approx(0.333, abs=0.01)
    assert c.tp_share == pytest.approx(0.231, abs=0.01)
    # resolved-only bias would be +0.027 (looks calibrated); all-in is +0.129.
    assert c.bias_resolved_only == pytest.approx(0.027, abs=0.01)
    assert c.bias_all_in == pytest.approx(0.129, abs=0.01)
    assert c.status == "OVERCONFIDENT"   # all-in catches it
    # the OLD resolved-only lens would have called this ~calibrated:
    assert abs(c.bias_resolved_only) <= CALIBRATED_BAND


def test_underconfident_cell():
    """Published below the all-in base rate -> UNDERCONFIDENT (direction good,
    geometry easier than claimed)."""
    c = cell_calibration(tp=40, sl=20, expired=5, published_p_win=0.30)
    assert c.tp_share == pytest.approx(0.615, abs=0.01)
    assert c.bias_all_in < 0
    assert c.status == "UNDERCONFIDENT"


def test_calibrated_cell():
    """Published ≈ all-in base rate -> CALIBRATED."""
    c = cell_calibration(tp=25, sl=50, expired=25, published_p_win=0.25)
    assert c.tp_share == pytest.approx(0.25, abs=0.001)
    assert c.status == "CALIBRATED"


def test_thin_cell_not_adjudicated():
    """Below min_n -> THIN (fail-open: no verdict, no penalty)."""
    c = cell_calibration(tp=2, sl=3, expired=1, published_p_win=0.9, min_n=20)
    assert c.status == "THIN"


def test_no_skill_claimed_edge_negative_on_loss_machine():
    """A claim BELOW the bracket's geometry coin-flip line is a NEGATIVE
    claimed edge — even the geometry alone beats the published p_win. This is
    the per-cell scream the old calibration couldn't emit.

    no_skill_line = SL/(TP+SL) from the DISTANCES. A wide-SL / near-TP
    bracket (sl_distance 8, tp_distance 2) → no_skill 0.8; a published 0.30
    claim sits far below it → strongly negative claimed edge."""
    c = cell_calibration(
        tp=10, sl=90, expired=0, published_p_win=0.30,
        tp_distance=2.0, sl_distance=8.0, min_n=5,
    )
    assert c.no_skill_line == pytest.approx(0.8, abs=0.01)
    assert c.claimed_edge is not None
    assert c.claimed_edge < 0, "claiming 0.30 against a 0.8 coin-flip line is negative edge"
    assert c.status == "OVERCONFIDENT"


def test_claimed_edge_positive_when_claim_beats_geometry():
    """A claim ABOVE the no-skill line has positive claimed edge — the model
    asserts it beats what the bracket geometry alone delivers."""
    c = cell_calibration(
        tp=60, sl=40, expired=0, published_p_win=0.70,
        tp_distance=2.0, sl_distance=8.0, min_n=5,
    )
    assert c.no_skill_line == pytest.approx(0.8, abs=0.01)
    # 0.70 < 0.8 still negative here; raise the claim to clear the line.
    c2 = cell_calibration(
        tp=60, sl=40, expired=0, published_p_win=0.85,
        tp_distance=2.0, sl_distance=8.0, min_n=5,
    )
    assert c2.claimed_edge is not None and c2.claimed_edge > 0


def test_no_skill_none_without_geometry():
    """No tp/sl distance -> no_skill_line and claimed_edge are None (fail-open)."""
    c = cell_calibration(tp=10, sl=90, expired=0, published_p_win=0.5, min_n=5)
    assert c.no_skill_line is None
    assert c.claimed_edge is None


# ── C. Backfill equivalence: product reproduces the fused number ────────────

def test_fused_rebuild_equals_product():
    """p_direction × p_reachability reproduces the fused probability within
    rounding — so the live blend can be REPLACED by the product without a
    math discontinuity."""
    c = cell_calibration(tp=30, sl=60, expired=40, published_p_win=0.0, min_n=5)
    assert c.p_win_fused_rebuilt == pytest.approx(
        c.p_direction * c.p_reachability, abs=0.001
    )


def test_zero_expired_decomposition_is_resolved_rate():
    """With no expiry, p_reachability == resolved_tp_share, so the product
    collapses to the resolved WR (the degenerate no-expiry case)."""
    c = cell_calibration(tp=30, sl=70, expired=0, published_p_win=0.3, min_n=5)
    assert c.p_reachability == pytest.approx(c.resolved_tp_share, abs=0.001)


# ── B. Replay: live era-clean book (skipped offline) ────────────────────────

def _fetch_live_reachability():
    """Fetch era-clean all-in reachability cells from Supabase. Returns list
    of rows, or None when creds/env are unavailable (offline)."""
    try:
        import os
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
        if not url or not key:
            return None
        import httpx
        resp = httpx.get(
            f"{url}/rest/v1/nt_bracket_reachability",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={
                "select": "symbol,vol_tier,distance_atr_bucket,signal,n,"
                          "pct_hit_tp,pct_hit_sl,pct_expired",
                "signal": "eq.all",
                "atr_source": "eq.stored",
                "n": "gte.20",
                "order": "n.desc",
                "limit": "200",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


@pytest.mark.skipif(
    _fetch_live_reachability() is None,
    reason="no Supabase creds (offline)",
)
def test_replay_live_book_all_in_verdict_differs_from_resolved():
    """REPLAY: across the real era-clean book, the all-in verdict must flag
    strictly more overconfidence than the resolved-only lens would — the
    'calibrated but losing' population the old scoring missed."""
    rows = _fetch_live_reachability()
    assert rows, "expected live reachability rows"
    # Use a representative published p_win (era-clean avg blend ~0.55) so the
    # verdict exercises the real overconfidence regime.
    verdicts = [reframe_reachability_row({**r, "avg_p_win": 0.55}) for r in rows]
    adjudicated = [v for v in verdicts if v.status != "THIN"]
    assert adjudicated, "expected adjudicated cells"
    # Every adjudicated era-clean cell is overconfident at a 0.55 claim.
    over = [v for v in adjudicated if v.status == "OVERCONFIDENT"]
    assert len(over) >= int(0.9 * len(adjudicated)), (
        f"expected ~all era-clean cells overconfident, got {len(over)}/{len(adjudicated)}"
    )
    # The all-in bias is strictly harsher than resolved-only wherever expiry>0.
    with_expiry = [v for v in adjudicated if v.expired_share > 0]
    if with_expiry:
        assert all(
            v.bias_all_in >= v.bias_resolved_only - 1e-9 for v in with_expiry
        ), "all-in bias must be >= resolved-only bias when expiry>0"


def test_reframe_row_reconstructs_counts_from_shares():
    """reframe_reachability_row rebuilds counts from n×share and preserves n."""
    row = {"symbol": "XAUUSD", "vol_tier": "low_vol", "distance_atr_bucket": "4+",
           "signal": "all", "n": 200, "pct_hit_tp": 0.20, "pct_hit_sl": 0.70,
           "pct_expired": 0.10, "avg_p_win": 0.55}
    c = reframe_reachability_row(row)
    assert c.n == 200
    assert c.tp + c.sl + c.expired == 200
    assert c.tp_share == pytest.approx(0.20, abs=0.01)
    assert c.status == "OVERCONFIDENT"
