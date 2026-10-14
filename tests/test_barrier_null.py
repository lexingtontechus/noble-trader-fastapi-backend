"""barrier.py sanity anchors — the analytic null must be physics, not vibes.

Pins the six invariants from the module docstring (gambler's ruin, symmetry,
normalization, scale invariance, Monte Carlo agreement, walk agreement with
``tiered_atr.walk_envelopes``) plus the continuous-geometry solver's
contract. If any of these breaks, the Phase B/C swaps built on top are
built on sand — so they run first, standalone, no network, no flags.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from regime_platform.sweep_orchestrator.barrier import (
    barrier_null,
    barrier_null_from_prices,
    ev_atr,
    ev_lower_bound_z,
    solve_geometry_pbm,
)
from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    walk_envelopes,
)

# ─── Anchors ────────────────────────────────────────────────────────────────


def test_gamblers_ruin_limit():
    """T -> inf: pi_W -> b/(a+b) — the random-entry breakeven the measured
    book currently sits BELOW (13-15% vs ~37.5% at the shipped bands)."""
    for a, b in [(1.0, 1.0), (2.0, 1.0), (4.94, 1.0), (1.2, 7.49)]:
        n = barrier_null(a, b, t_bars=250_000, sigma=1.0)
        assert n.p_win == pytest.approx(b / (a + b), abs=2e-3), (a, b, n)
        # and via the fallback branch (sigma*sqrt(T)/L > 200)
        nf = barrier_null(a, b, t_bars=10**9, sigma=1.0)
        assert nf.p_win == pytest.approx(b / (a + b), abs=1e-9), (a, b, nf)


def test_symmetry():
    for a, b in [(1.0, 1.0), (2.0, 1.0), (4.0, 2.0)]:
        n = barrier_null(a, b, t_bars=EXPIRY_BARS)
        m = barrier_null(b, a, t_bars=EXPIRY_BARS)
        assert n.p_win == pytest.approx(m.p_loss, abs=1e-12)
        assert n.p_loss == pytest.approx(m.p_win, abs=1e-12)
    assert barrier_null(1.0, 1.0).p_win == pytest.approx(0.5, abs=1e-12)


def test_normalization_exact():
    rng = np.random.default_rng(7)
    for _ in range(300):
        a = float(rng.uniform(0.3, 16.0))
        b = float(rng.uniform(0.3, 8.0))
        t = int(rng.choice([12, 24, 48, 96]))
        s = float(rng.uniform(0.5, 1.5))
        n = barrier_null(a, b, t_bars=t, sigma=s)
        assert n.p_win >= 0.0 and n.p_loss >= 0.0 and n.p_expire >= 0.0
        assert n.p_win + n.p_loss + n.p_expire == pytest.approx(1.0, abs=1e-9)


def test_scale_invariance():
    """Distances enter only through a/(sigma*sqrt(T)) — proposal §2's
    normalization anchor (4.5-ATR TP against 48 bars is 0.65 horizon-normalized).
    Scaling that preserves a/(sigma*sqrt(T)): (a,b,sigma) -> (ka,kb,k*sigma) at
    fixed T, or (a,b) -> (ka,kb) with T -> k^2*T at fixed sigma."""
    n1 = barrier_null(4.5, 2.0, t_bars=48, sigma=1.0)
    n2 = barrier_null(9.0, 4.0, t_bars=48, sigma=2.0)
    n3 = barrier_null(2.25, 1.0, t_bars=48, sigma=0.5)
    n4 = barrier_null(9.0, 4.0, t_bars=192, sigma=1.0)
    for other in (n2, n3, n4):
        assert n1.p_win == pytest.approx(other.p_win, abs=1e-12)
        assert n1.p_loss == pytest.approx(other.p_loss, abs=1e-12)


def test_deadline_monotone():
    """More time -> strictly more resolved mass until saturation (then it
    must HOLD, never reverse), and pi_W rises toward the ruin anchor."""
    a, b = 4.5, 2.0
    prev_resolve = 0.0
    prev_win = 0.0
    for t in (6, 12, 24, 48, 96):
        n = barrier_null(a, b, t_bars=t)
        resolve = n.p_win + n.p_loss
        assert resolve > prev_resolve + 1e-9
        assert n.p_win > prev_win + 1e-9
        prev_resolve, prev_win = resolve, n.p_win
    # saturation: resolve is within epsilon of 1 and never exceeds it
    for t in (192, 768, 250_000):
        n = barrier_null(a, b, t_bars=t)
        assert n.p_win + n.p_loss >= prev_resolve
        assert n.p_win + n.p_loss <= 1.0 + 1e-9
    assert prev_win < b / (a + b) + 1e-9  # never exceeds the no-deadline anchor


def test_degenerate_inputs_fail_honest():
    n = barrier_null(0.0, 2.0)
    assert n.p_expire == 1.0
    n = barrier_null(float("nan"), 2.0)
    assert n.p_expire == 1.0
    n = barrier_null(1.0, 1.0, t_bars=0)
    assert n.p_expire == 1.0


# ─── Monte Carlo agreement ──────────────────────────────────────────────────


@pytest.mark.parametrize("a,b,t", [(1.0, 1.0, 48), (4.5, 2.0, 48), (2.0, 0.8, 24)])
def test_monte_carlo_agreement(a: float, b: float, t: int):
    """Sub-stepped (near-continuously monitored) driftless BM must reproduce
    the closed form. NOTE: bar-END monitoring is deliberately NOT used — it
    carries the classic discretization bias toward the FARTHER barrier
    (missed intrabar touches of the near barrier free those paths to win
    later), which for (4.5, 2, 48) inflates pi_W from 0.306 to 0.328. The
    closed form models continuous monitoring; so must the reference."""
    rng = np.random.default_rng(42)
    sub = 25
    n_paths = 60_000
    total = t * sub
    steps = rng.standard_normal((n_paths, total)) / math.sqrt(sub)
    x = np.zeros(n_paths)
    ta = np.full(n_paths, total + 1)
    tb = np.full(n_paths, total + 1)
    for i in range(total):
        x += steps[:, i]
        na = (x >= a) & (ta > total)
        nb = (x <= -b) & (tb > total)
        ta[na] = i
        tb[nb] = i
    won = (ta < tb) & (ta <= total)
    los = (tb < ta) & (tb <= total)
    n = barrier_null(a, b, t_bars=t)
    mc_w = float(won.mean())
    mc_l = float(los.mean())
    # tolerance = 4-sigma MC error + sub-step residual touch bias
    tol = 4 * math.sqrt(0.25 / n_paths) + 0.012
    assert mc_w == pytest.approx(n.p_win, abs=tol), (mc_w, n.p_win)
    assert mc_l == pytest.approx(n.p_loss, abs=tol), (mc_l, n.p_loss)


# ─── Walk agreement — the analytic null vs the platform's own simulator ─────


def test_walk_envelope_agreement():
    """walk_envelopes on TRUE OHLC bars (high/low of a sub-stepped BM) must
    sit close to the closed form. The envelope walk IS the acceptance-test
    simulator for the analytic layer (proposal §7); this pins the standing
    agreement on synthetic data so real-data replay drift gets caught.
    Asymmetric brackets keep a small far-barrier ordering bias (the bar's
    high/low still misses the sub-step interior), so the asymmetric case is
    checked within a wider relative band and for DIRECTION."""
    rng = np.random.default_rng(11)
    sub = 8
    n_paths, t = 40_000, EXPIRY_BARS
    fine = rng.standard_normal((n_paths, t * sub)) / math.sqrt(sub)
    cum = np.cumsum(fine, axis=1).reshape(n_paths, t, sub)
    hi = cum.max(axis=2)   # bar high
    lo = cum.min(axis=2)   # bar low
    env = np.stack([hi, lo], axis=2)  # (n, t, 2) favourable/adverse for a long
    for a, b, rel_w, rel_r in [(1.0, 1.0, 0.05, 0.03), (2.0, 1.0, 0.10, 0.05)]:
        pnl, expired = walk_envelopes(env, a, b)
        n = len(env)
        walk_w = float(((pnl > 0) & ~expired).sum()) / n
        walk_l = float(((pnl < 0) & ~expired).sum()) / n
        closed = barrier_null(a, b, t_bars=t, sigma=1.0)
        assert walk_w == pytest.approx(closed.p_win, rel=rel_w), (a, b, walk_w, closed.p_win)
        assert (walk_w + walk_l) == pytest.approx(
            closed.p_win + closed.p_loss, rel=rel_r
        ), (a, b, walk_w + walk_l, closed.p_win + closed.p_loss)


# ─── EV and the gate ────────────────────────────────────────────────────────


def test_ev_convention_matches_picker():
    """EV = p_W*a - p_L*b + p_E*m - c — the walk's row convention
    (net = pnl - cost) in expectation, with expire never a loss."""
    ev = ev_atr(0.4, 0.35, 0.25, 3.0, 1.0, cost_atr=0.1)
    assert ev == pytest.approx(0.4 * 3.0 - 0.35 * 1.0 - 0.1)
    # all-expire bracket pays only the cost
    assert ev_atr(0.0, 0.0, 1.0, 3.0, 1.0, cost_atr=0.1) == pytest.approx(-0.1)


def test_ev_sign_arithmetic():
    """A p_W=0.056 bracket is blocked by arithmetic, not a bolt-on gate —
    the XAUAUD class from the triage, priced by the null at the widened band."""
    n = barrier_null(6.82, 4.31)  # XAUAUD tp=6.82 cell, dead bucket
    ev = n.ev_atr(cost_atr=0.1)
    # ruin odds for this geometry: 4.31/11.13 = 0.387; the deadline-bound
    # null is lower; either way, after costs this must be clearly negative.
    assert ev < 0.0


def test_price_wrapper_directions():
    # Sell prices are the exact log-space mirror of the buy bracket
    # (100^2/103, 100^2/99) so both sides price the same distances.
    n_buy = barrier_null_from_prices(100.0, 103.0, 99.0, atr=1.0, direction="buy")
    n_sell = barrier_null_from_prices(
        100.0, 100.0**2 / 103.0, 100.0**2 / 99.0, atr=1.0, direction="sell",
    )
    assert n_buy.p_win == pytest.approx(n_sell.p_win, abs=1e-9)
    assert n_buy.p_loss == pytest.approx(n_sell.p_loss, abs=1e-9)
    assert n_buy.tp_atr == pytest.approx(n_sell.tp_atr, abs=1e-6)
    assert n_buy.sl_atr == pytest.approx(n_sell.sl_atr, abs=1e-6)
    # exact log distances, first-order ATR normalization
    assert n_buy.tp_atr == pytest.approx(math.log(1.03) * 100.0, abs=1e-9)
    assert n_buy.sl_atr == pytest.approx(math.log(100.0 / 99.0) * 100.0, abs=1e-9)
    assert n_sell.tp_atr == pytest.approx(math.log(1.03) * 100.0, abs=1e-6)
    assert n_sell.sl_atr == pytest.approx(math.log(100.0 / 99.0) * 100.0, abs=1e-6)
    # a broken bracket (sell with TP above entry) fails honest: all-expire
    n_bad = barrier_null_from_prices(100.0, 103.0, 101.0, atr=1.0, direction="sell")
    assert n_bad.p_expire == 1.0


def test_ev_lower_bound_z():
    assert ev_lower_bound_z(0.05) == pytest.approx(1.6449, abs=1e-4)
    assert ev_lower_bound_z(0.5) == pytest.approx(0.0, abs=1e-3)


# ─── Continuous geometry solver ─────────────────────────────────────────────


def test_solve_geometry_matches_grid_on_null():
    """With no tilt, the continuous argmax must agree with an exhaustive
    fine grid to within the refinement tolerance — the property that lets
    it REPLACE the 9-point grid (D2) without changing what 'best' means."""
    tp_lo, tp_hi, sl_lo, sl_hi = 3.0, 6.0, 0.8, 3.0
    a, b, ev, _ = solve_geometry_pbm(
        tp_lo=tp_lo, tp_hi=tp_hi, sl_lo=sl_lo, sl_hi=sl_hi,
        min_rr=1.0, cost_atr=0.1, step=0.25,
    )
    # exhaustive reference on a 0.05 lattice
    best_ref = None
    aa = tp_lo
    while aa <= tp_hi + 1e-9:
        bb = sl_lo
        while bb <= sl_hi + 1e-9:
            if bb > 0 and aa / bb >= 1.0 - 1e-9:
                ev_ref = barrier_null(aa, bb).ev_atr(cost_atr=0.1)
                if best_ref is None or ev_ref > best_ref[0]:
                    best_ref = (ev_ref, aa, bb)
            bb += 0.05
        aa += 0.05
    assert best_ref is not None
    assert ev >= best_ref[0] - 1e-6  # never below the fine-grid optimum
    assert a == pytest.approx(best_ref[1], abs=0.13)
    assert b == pytest.approx(best_ref[2], abs=0.13)


def test_solve_geometry_respects_domain_and_rr():
    # min_rr prunes the whole domain -> honest degenerate return
    out = solve_geometry_pbm(
        tp_lo=1.0, tp_hi=2.0, sl_lo=5.0, sl_hi=6.0,
        min_rr=3.0, cost_atr=0.1,
    )
    assert out[2] == float("-inf")
    # otherwise the solution respects clamps and rr
    a, b, _, _ = solve_geometry_pbm(
        tp_lo=2.0, tp_hi=5.0, sl_lo=1.0, sl_hi=4.0,
        min_rr=2.0, cost_atr=0.1,
    )
    assert 2.0 <= a <= 5.0 and 1.0 <= b <= 4.0 and (a / b) >= 2.0 - 1e-6


def test_solve_geometry_tilt_selects_favourable_bucket():
    """The solver must optimize the objective it is GIVEN: with a tilt that
    kills win mass for wide TP, the chosen point's tilted-EV must beat the
    pure-null argmax's tilted-EV (and the tilt itself must actually bite —
    checked via the exhaustive reference on the tilted objective)."""

    def tilt(a: float, b: float):
        n = barrier_null(a, b)
        if a > 4.0:  # measured dead region: keep only 40% of null win mass
            return (n.p_win * 0.4, n.p_loss, 1.0 - n.p_win * 0.4 - n.p_loss)
        return (n.p_win, n.p_loss, n.p_expire)

    def ev_tilt(a: float, b: float) -> float:
        p_w, p_l, p_e = tilt(a, b)
        return ev_atr(p_w, p_l, p_e, a, b, cost_atr=0.1)

    kw = dict(tp_lo=2.0, tp_hi=6.0, sl_lo=1.0, sl_hi=2.0, min_rr=1.0, cost_atr=0.1)
    a_t, b_t, ev_t, _ = solve_geometry_pbm(**kw, tilt=tilt)
    a_p, b_p, _, _ = solve_geometry_pbm(**kw)  # null argmax

    # 1. the tilted solver's point beats the null argmax under the tilted objective
    assert ev_t >= ev_tilt(a_p, b_p) - 1e-9
    # 2. it matches an exhaustive reference on the tilted objective
    best_ref = None
    aa = 2.0
    while aa <= 6.0 + 1e-9:
        bb = 1.0
        while bb <= 2.0 + 1e-9:
            if aa / bb >= 1.0 - 1e-9:
                e = ev_tilt(aa, bb)
                if best_ref is None or e > best_ref[0]:
                    best_ref = (e, aa, bb)
            bb += 0.05
        aa += 0.05
    assert best_ref is not None
    assert ev_t >= best_ref[0] - 1e-6
    assert a_t == pytest.approx(best_ref[1], abs=0.13)
    # 3. the tilt genuinely bites: the tilted argmax escapes the dead region
    #    whenever a live region can compete (exhaustive ref says it can)
    if best_ref[1] <= 4.0:
        assert a_t <= 4.0 + 0.13
