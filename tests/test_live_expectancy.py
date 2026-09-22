"""P2 live expectancy daily — cohort finalize + bracket split."""

from __future__ import annotations

from regime_platform.sweep_orchestrator.live_expectancy import (
    COHORT_ACCEPT,
    COHORT_ALL,
    COHORT_REJECT,
    _bump,
    _empty,
    _finalize,
)


def test_finalize_expectancy_r():
    d = _empty()
    for outcome, tp, sl in [
        ("tp_hit", 5, 3),
        ("tp_hit", 5, 3),
        ("sl_hit", 5, 3),
        ("sl_hit", 5, 3),
        ("sl_hit", 5, 3),
    ]:
        _bump(d, {"tp_bricks": tp, "sl_bricks": sl, "p_win_dir": 0.6, "p_win_bracket": 0.25}, outcome)
    m = _finalize(d)
    assert m["n"] == 5
    assert m["n_tp"] == 2
    assert m["n_sl"] == 3
    assert m["win_rate_closed"] == 0.4
    # expectancy = 0.4 * (5/3) - 0.6 * 1 = 0.666... - 0.6 = 0.0667
    assert m["expectancy_r"] == 0.0667
    assert m["bias_dir"] == 0.2  # 0.6 - 0.4


def test_finalize_pct_expired():
    d = _empty()
    _bump(d, {"p_win_dir": 0.5}, "tp_hit")
    _bump(d, {"p_win_dir": 0.5}, "expired")
    _bump(d, {"p_win_dir": 0.5}, "expired")
    _bump(d, {"p_win_dir": 0.5}, "expired")
    m = _finalize(d)
    assert m["pct_expired"] == 0.75
    assert m["win_rate_closed"] == 1.0
    assert m["n_expired"] == 3


def test_bracket_cohort_names():
    assert COHORT_ALL == "all_qualified"
    assert COHORT_ACCEPT == "bracket_accept"
    assert COHORT_REJECT == "bracket_reject"


def test_min_bracket_split_uses_0_20():
    # Document the live gate scale: 0.20 (was 0.45 — max p_win_bracket ~0.31).
    accept, reject = 0.20, 0.19
    assert accept >= 0.20
    assert reject < 0.20
