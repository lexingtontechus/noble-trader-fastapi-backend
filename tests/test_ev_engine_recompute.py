"""
Tests for EVEngine.recompute_ev_fields() — fix #1 from
worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md ("stale derived fields
after calibration enforcement").

Calibration enforcement shrinks p_win toward 0.5 AFTER compute_ev() already
ran. recompute_ev_fields() re-derives ev/ev_per_dollar/rr/allowed/ev_scale/
aggression from the corrected p_win so the EVFullResult object never carries
a p_win that's inconsistent with its own derived fields.
"""

from regime_platform.services.statistics.ev_engine import EVEngine


def test_recompute_matches_compute_ev_for_same_inputs():
    """recompute_ev_fields(p_win, profit, stake) must agree with what
    compute_ev() itself would produce for the same effective p_win — i.e.
    it's not a divergent formula, just a way to re-derive post-hoc."""
    engine = EVEngine(min_ev=0.0, min_rr=1.5)
    profit = 5.0 * 0.5   # tp_bricks * brick_size
    stake = 3.0 * 0.5    # sl_bricks * brick_size

    for p_win in (0.98, 0.85, 0.65, 0.55, 0.50, 0.40):
        result = engine.recompute_ev_fields(p_win, profit, stake)
        expected_ev = (p_win * profit) - ((1.0 - p_win) * stake)
        expected_rr = profit / stake
        expected_ev_per_dollar = expected_ev / stake
        assert result["ev"] == round(expected_ev, 4)
        assert result["rr"] == round(expected_rr, 4)
        assert result["ev_per_dollar"] == round(expected_ev_per_dollar, 4)
        assert result["allowed"] == ((expected_ev > 0.0) and (expected_rr >= 1.5))
        assert result["aggression"] == engine.compute_aggression(expected_ev_per_dollar)
        assert result["ev_scale"] == round(engine.compute_ev_scale(expected_ev_per_dollar), 4)


def test_recompute_reflects_bayesian_shrink_not_stale_pre_shrink_value():
    """The whole point of the fix: recomputing with the SHRUNK p_win must
    produce different ev/aggression than the pre-shrink value would have —
    otherwise the bug (stale fields) is still effectively present."""
    engine = EVEngine(min_ev=0.0, min_rr=1.5)
    profit, stake = 2.5, 1.5  # RR = 1.667

    pre_shrink_p_win = 0.98
    post_shrink_p_win = 0.50  # e.g. bayesian_adjusted_p_win() output, shrunk toward the 0.5 prior

    pre = engine.recompute_ev_fields(pre_shrink_p_win, profit, stake)
    post = engine.recompute_ev_fields(post_shrink_p_win, profit, stake)

    assert pre["ev"] != post["ev"]
    assert pre["ev_per_dollar"] != post["ev_per_dollar"]
    # High p_win (0.98) -> high ev_per_dollar -> "passive"; shrunk p_win (0.55)
    # -> much lower ev_per_dollar -> should drop out of "passive" aggression.
    assert pre["aggression"] == "passive"
    assert post["aggression"] != "passive"


def test_recompute_allowed_flips_when_shrunk_ev_goes_non_positive():
    """When enforcement shrinks p_win enough that ev turns non-positive,
    `allowed` must flip to False on the recomputed fields — this is exactly
    the case the stale-field bug hid (the gate previously kept reading the
    pre-shrink `allowed=True`)."""
    engine = EVEngine(min_ev=0.0, min_rr=1.5)
    profit, stake = 1.0, 1.0  # RR = 1.0 (below MIN_RR=1.5, but isolate ev check)

    high = engine.recompute_ev_fields(0.90, profit, stake)
    low = engine.recompute_ev_fields(0.50, profit, stake)  # ev = 0.5*1 - 0.5*1 = 0.0

    assert high["ev"] > 0.0
    assert low["ev"] == 0.0
    assert low["allowed"] is False  # ev must be STRICTLY > min_ev


def test_recompute_ev_scale_and_aggression_use_shared_static_methods():
    """recompute_ev_fields must delegate to the same compute_aggression /
    compute_ev_scale used by compute_ev() itself, not a duplicated formula —
    guards against the two paths drifting apart in the future."""
    engine = EVEngine()
    result = engine.recompute_ev_fields(p_win=0.75, profit=3.0, stake=2.0)
    ev_per_dollar = result["ev_per_dollar"]
    assert result["aggression"] == EVEngine.compute_aggression(ev_per_dollar)
    assert result["ev_scale"] == round(EVEngine.compute_ev_scale(ev_per_dollar), 4)
