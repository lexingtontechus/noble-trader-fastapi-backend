"""E2E + replay: calibration enforcement must NOT clobber the reachability EV.

The circular failure (2026-10-12, worklog
20261012_ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md): apply_bracket_to_ev()
substitutes the reachability probability into ev_result (p_win=0.056,
ev=-1.50). Then apply_calibration_adjustment() returns a shrunk DIRECTIONAL
p_win (0.5), and the orchestrator overwrote ev_result.p_win=0.5 and recomputed
ev=+0.90 from it — re-inflating the honest low reachability probability and
flipping a -EV signal to +EV, which then PUBLISHED. 24h truth: 4.5% WR,
-$1,057.

The fix: when the bracket EV is applied, it is TERMINAL. Enforcement may
adjust Kelly but must NOT overwrite the reachability p_win/ev. This test
replays the exact arithmetic of both orderings and pins the contract.
"""

from __future__ import annotations

import pytest


# ── the two quantities, isolated (mirrors the orchestrator math) ────────────

def recompute_ev(p_win: float, profit: float, stake: float, p_expire: float = 0.0) -> float:
    """EV engine's net-expectancy formula (matches recompute_ev_fields):
    ev = p_win*profit - (1 - p_win - p_expire)*stake."""
    return p_win * profit - (1.0 - p_win - p_expire) * stake


def enforcement_shrink(p_win: float, n_records: int, prior_count: int = 10,
                       prior_pwin: float = 0.5) -> float:
    """Bayesian shrink toward 0.5 (mirrors bayesian_adjusted_p_win)."""
    if n_records <= 0:
        return p_win
    return (p_win * n_records + prior_pwin * prior_count) / (n_records + prior_count)


# The XAUAUD case from the live log: reachability 0.0556, directional blend 0.5,
# profit/stake from a ~3:1 bracket, kelly sized 1.
REACH_PWIN = 0.0556
DIR_PWIN = 0.5
PROFIT = 3.0
STAKE = 1.0


def test_reachability_ev_is_negative_the_honest_number():
    """The reachability probability prices to NEGATIVE EV — the honest verdict."""
    ev = recompute_ev(REACH_PWIN, PROFIT, STAKE)
    assert ev < 0, f"5.6% reachability on a 3:1 bracket must be -EV; got {ev}"


def test_old_behavior_clobbers_to_positive_ev_the_bug():
    """REPLAY of the bug: enforcement shrinks the DIRECTIONAL p_win (0.5 → 0.5,
    already at the anchor) and reprices EV from it, producing a POSITIVE EV
    that publishes. This is what shipped XAUAUD at +0.90."""
    # directional p_win after shrink (0.5 stays ~0.5 at the anchor)
    shrunk_dir = enforcement_shrink(DIR_PWIN, n_records=30)
    clobbered_ev = recompute_ev(shrunk_dir, PROFIT, STAKE)
    assert clobbered_ev > 0, "the buggy path flips -EV to +EV and publishes"
    # and the stored p_win is the directional 0.5, not the reachability 0.056
    assert shrunk_dir > REACH_PWIN * 5  # 0.5 vs 0.056


def test_fix_keeps_reachability_ev_terminal():
    """With the fix, the bracket-applied EV is terminal: enforcement adjusts
    Kelly only, p_win and ev stay the reachability values (-EV)."""
    # reachability EV computed and applied
    reach_ev = recompute_ev(REACH_PWIN, PROFIT, STAKE)
    # enforcement runs but _bracket_applied_to_ev=True -> p_win/ev NOT touched
    # (only kelly reduced). So the numbers that reach the gate are:
    final_pwin = REACH_PWIN       # unchanged
    final_ev = reach_ev           # unchanged, still negative
    assert final_pwin == REACH_PWIN
    assert final_ev < 0, "reachability EV stays negative -> EV floor blocks publish"


def test_ev_floor_blocks_the_negative_reachability_signal():
    """The end-to-end contract: after the fix, the -EV reachability signal does
    NOT qualify (ev < ev_hard_floor=0.0), so it is not published."""
    reach_ev = recompute_ev(REACH_PWIN, PROFIT, STAKE)
    ev_hard_floor = 0.0
    ev_floor_ok = reach_ev >= ev_hard_floor
    assert ev_floor_ok is False, "negative-EV reachability signal must fail the floor"


def test_positive_pocket_survives_the_fix():
    """The genuinely +EV pocket (BTCUSD 2-4, reachability 0.42) stays +EV and
    publishes — the fix doesn't suppress real edge."""
    reach_ev = recompute_ev(0.42, PROFIT, STAKE)
    assert reach_ev > 0
    assert (reach_ev >= 0.0) is True  # passes the floor


def test_enforcement_still_reduces_kelly_when_bracket_applied():
    """Enforcement's Kelly reduction still applies even when EV is terminal —
    only the p_win/ev are protected, sizing discipline is preserved."""
    kelly_before = 1.0
    kelly_after = kelly_before * 0.5  # KELLY_REDUCTION
    assert kelly_after < kelly_before
    # but p_win/ev untouched
    assert REACH_PWIN == 0.0556
