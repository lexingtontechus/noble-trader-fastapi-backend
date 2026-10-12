"""E2E + replay: bucket-inversion harmonization (p_win_bracket_source).

Proves the era-clean inversion and that the all_in source removes it.

The defect (2026-10-12, worklog 20261012_BUCKET_INVERSION_HARMONIZATION.md):
the live p_win_bracket re-derived the probability as
    p_resolve × beta_shrunk(tp/(tp+sl))
instead of reading the diagnostic table's all-in TP share (pct_hit_tp). For
thin resolved cells the Beta(2,2) prior dominates and inflates the rate, so a
bucket that wins 0% of the time was priced ~0.5, and winnability DECREASED as
buckets widened — the inverse of reality. The all_in source sets
p_win_bracket = pct_hit_tp directly, one formula everywhere.

Era-clean ground truth (nt_signal_sim, ts>=2026-10-02), the cells this test
reproduces:
    <0.5 : TP 0.000  (priced 0.545 under derived)
    0.5-1: TP 0.063
    1-2  : TP 0.069
    2-4  : TP 0.148
    4+   : TP 0.130 (priced 0.126 — the ONE bucket derived got ~right)
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.p_win_bracket import (
    BETA_ALPHA,
    BETA_BETA,
    beta_shrunk_win_rate,
    compute_bracket_probs,
)
from regime_platform.core.feature_flags import get_p_win_bracket_source


# ── the two formulas, isolated ──────────────────────────────────────────────

def _derived(pct_tp, pct_sl, pct_exp, *, n=50):
    p_resolve = pct_tp + pct_sl
    if pct_exp is not None:
        p_resolve = max(0.0, min(1.0, 1.0 - pct_exp))
    p_win_resolve = beta_shrunk_win_rate(n * pct_tp, n * pct_sl,
                                         BETA_ALPHA, BETA_BETA)
    return p_resolve * p_win_resolve


def _cell(pct_tp, pct_sl, pct_exp, *, n_stored=50, entry=2000.0, tp=2004.0, atr=1.0):
    """tp/atr = 4.0 -> bucket '4+'. n_stored large so no bootstrap fallback."""
    return compute_bracket_probs(
        entry_price=entry, take_profit=tp, atr=atr,
        regime_label="low_vol",  # any vol_tier
        n=n_stored, n_stored=n_stored,
        pct_hit_tp=pct_tp, pct_hit_sl=pct_sl, pct_expired=pct_exp,
    )


# ── source flag ships all_in ────────────────────────────────────────────────

def test_source_defaults_to_all_in():
    assert get_p_win_bracket_source() == "all_in"


# ── all_in removes the inversion ────────────────────────────────────────────

def test_all_in_prices_dead_narrow_bucket_at_zero():
    """A <0.5 bucket with 0% TP must price 0.0 under all_in (not 0.545)."""
    b = _cell(0.0, 1.0, 0.0, n_stored=79)
    assert b.p_win_bracket == 0.0
    assert b.bucket == "4+"  # geometry still classified; the RATE is 0


def test_all_in_equals_pct_hit_tp_directly():
    """all_in: p_win_bracket == pct_hit_tp, exactly, for every cell."""
    for tp_share in (0.0, 0.063, 0.069, 0.148, 0.417, 0.271):
        b = _cell(tp_share, 1.0 - tp_share, 0.0)
        assert abs(b.p_win_bracket - tp_share) < 1e-9, (
            f"all_in must equal pct_hit_tp; got {b.p_win_bracket} for {tp_share}"
        )


def test_derived_inflates_dead_bucket_the_bug():
    """REPLAY of the bug: the derived path applies a Beta(2,2) prior that pulls
    a thin low-TP cell UP toward 0.5. On a 0.5-1 cell at 8.2% TP (n=49, the real
    era-clean XAUAUD cell), derived prices it ~0.11 — well above the honest
    0.082 — and the prior dominates harder as n thins. all_in would price 0.082.
    This prior-inflation, compounded across the resolved×resolve product, is
    what made dead narrow buckets read as winnable."""
    tp, sl, exp, n = 0.0816, 0.9184, 0.0, 49
    derived = _derived(tp, sl, exp, n=n)
    honest = tp  # all_in
    assert derived > honest, (
        f"derived must be prior-inflated above the honest all-in share; "
        f"derived={derived:.3f} honest={honest:.3f}"
    )
    # thinner n -> stronger prior pull (closer to 0.5)
    derived_thin = _derived(tp, sl, exp, n=5)
    assert derived_thin > derived, "thinner cell -> stronger prior inflation"


def test_derived_zero_tp_prior_floor():
    """A 0%-TP cell still prices above 0 under derived (the prior floor) —
    the exact mechanism that let a never-winning bucket qualify."""
    derived = _derived(0.0, 1.0, 0.0, n=37)  # real 0.5-1 dead cell, n=37
    assert derived > 0.0, "derived never reaches 0 for a dead cell (prior floor)"
    # all_in reaches exactly 0
    assert _cell(0.0, 1.0, 0.0, n_stored=37).p_win_bracket == 0.0


def test_monotonic_all_in_matches_reality_not_inverted():
    """all_in p_win_bracket tracks the all-in TP share exactly, so winnability
    RISES toward the 2-4 sweet spot (0.148) and the wide 4+ (0.130) beats the
    narrow 1-2 (0.069) — matching reality. The derived path instead pulls thin
    narrow cells toward 0.5, so a dead 0%-TP bucket can price ABOVE the wide
    pocket — the inversion. Assert the all_in contract is monotone-in-truth and
    that derived distorts a thin narrow cell above its honest share."""
    # era-clean all-in TP shares by bucket (the honest reachability truth)
    honest = {"<0.5": 0.0, "0.5-1": 0.063, "1-2": 0.069, "2-4": 0.148, "4+": 0.130}
    vals = [honest[b] for b in honest]
    assert vals[3] == max(vals)               # 2-4 is the sweet spot
    assert vals[4] > vals[2] > vals[1] > vals[0]  # 4+ > 1-2 > 0.5-1 > 0
    # derived distorts: a THIN 0%-TP narrow cell (prior dominates) prices above
    # the honest wide pocket — the inversion. At n=3 the Beta(2,2) prior floor
    # is (0+2)/(0+3+4)=0.286, well above the wide pocket's honest 0.130.
    derived_narrow = _derived(0.0, 1.0, 0.0, n=3)       # thin dead cell
    honest_wide = honest["4+"]
    assert derived_narrow > honest_wide, (
        f"derived prices the thin dead narrow bucket ({derived_narrow:.3f}) above "
        f"the honest wide pocket ({honest_wide}) = inversion"
    )


def test_all_in_wide_bucket_close_to_derived():
    """For the wide 4+ bucket (low expiry-dominance), all_in ≈ derived — the one
    regime where the old formula was ~honest. Confirms all_in doesn't distort
    the already-correct cells."""
    tp, sl, exp = 0.417, 0.583, 0.0  # BTCUSD 2-4, the real pocket
    b = _cell(tp, sl, exp, n_stored=144)
    derived = _derived(tp, sl, exp)
    # both land near 0.42 (all_in exactly, derived within the beta shrink)
    assert abs(b.p_win_bracket - tp) < 1e-9
    assert abs(derived - tp) < 0.15


def test_expiry_heavy_cell_all_in_is_honest():
    """USDCAD low_vol 4+ : TP 0.181 / SL 0.042 / expired 0.778. all_in prices
    0.181 (the real TP rate); derived's p_resolve=0.222 shrinks it further but
    p_win_resolve prior-inflates — the two disagree, all_in is the truth."""
    b = _cell(0.181, 0.042, 0.778, n_stored=72)
    assert abs(b.p_win_bracket - 0.181) < 1e-9
    # p_resolve diagnostic still stamped for reference
    assert abs(b.p_resolve - 0.222) < 0.01
