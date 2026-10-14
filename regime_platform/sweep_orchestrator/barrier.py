"""
barrier.py — the analytic null: closed-form double-barrier first-passage.
═══════════════════════════════════════════════════════════════════════════════

Posterior Barrier Model, Layer 1 (`worklog/20261014_BARRIER_MODEL_INTEGRATION_
MAP_AND_PHASES.md` §4; proposal `20261013_POSTERIOR_PRICING_METHODOLOGY_
PROPOSAL.md` §2).

The thesis: for a driftless random walk, the probability that price touches
TP before SL before the deadline is **classical physics** — the two-sided
first-passage distribution of Brownian motion, available in closed form via
the method of images. It prices ANY geometry in microseconds: no grid, no
path floor, no cold-start. Every cell of the book gets an honest price the
moment it exists.

Not to be confused with ``first_passage.py`` (Phase 1b of the HMM work):
that module scores EMPIRICAL close-path arrays offline (its header: "LIVE
WIRING: none — ``walk()`` needs a full 48-bar window"). This module is its
analytic counterpart: it needs only ``(tp_atr, sl_atr, T, sigma)``, all of
which exist at signal time — which is precisely why it CAN be the live prior
and the path overlay cannot. Two objects, two names, two jobs; neither
imports the other.

Model
-----
Log-price as driftless BM with per-bar standard deviation ``sigma`` (in ATR
units, so distances are too). Drift is deliberately zero: every directional
candidate the platform has measured sits at or below chance (AUC 0.499 on
74k resolved signals) — the null IS the honest price, and the empirical
posterior layer exists to move cells off it only when outcomes say so.

For barriers ``+a`` (favourable) and ``-b`` (adverse), ``L = a + b``:

    pi_W = sum_k  sign(a + 2kL) * 2*Phi(-|a + 2kL| / (sigma*sqrt(T)))
    pi_L = sum_k  sign(-b - 2kL) * 2*Phi(|b + 2kL|... (same with roles swapped)
    pi_E = 1 - pi_W - pi_L

Each image term integrates the first-passage density of an effective
single barrier at distance |h_k|; the sign carries the absorbing
interaction between the two barriers. The series converges
super-exponentially once ``2|k|L >> sigma*sqrt(T)`` — single-digit terms in
this platform's regime (L is 7.6-15.6 ATR against a sqrt(48) ~ 6.9 horizon).

Sanity anchors (pinned by tests/test_barrier_null.py):
  * gambler's ruin  — T -> inf:  pi_W -> b / (a + b)
  * symmetry        — a == b:    pi_W == pi_L
  * normalization   — pi_W + pi_L + pi_E == 1 exactly
  * scale invariance — (a, b, sigma) ~ (ka, kb, k*sigma) identical
  * Monte Carlo agreement on simulated BM
  * walk agreement   — ``tiered_atr.walk_envelopes`` vs ``barrier_null`` on
    the same synthetic envelopes (the standing acceptance test that keeps
    the envelope walk honest as a SIMULATOR, not a selector)

Units
-----
Everything is ATR-normalized, matching ``walk_envelopes``'s convention
(pnl in ATR: +tp_atr win, -sl_atr loss, 0 expire). EV follows the picker's
own convention ``net = pnl - cost_atr`` per row, i.e. cost is charged once
per trade regardless of outcome:

    EV = p_W*a - p_L*b + p_E*m - c        (m = E[MTM|expire], 0 under the null)

``sigma=1.0`` default is the proposal's ``sigma = ATR_true_range / price``
expressed in ATR units (by construction one ATR per bar of standard
deviation). The empirical layer absorbs the systematic deviation of that
assumption — that is its job.

Pure functions only — no I/O, no clock, no flags. Deps: numpy + math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS

# Post-hoc normal quantile for the 5% lower bound (two-sided normal approx;
# the Dirichlet posterior's EV moment math lives in posterior.py).
_Z_05 = 1.6448536269514722

# Series truncation: |k| beyond this is unreachable for any sane band.
_K_MAX = 4096
# Above this sigma*sqrt(T)/L ratio the alternating images cancel to O(1)
# through thousands of +-1 terms; the no-deadline limit is numerically exact
# and infinitely cheaper. (This regime never occurs on this platform —
# sigma*sqrt(48) ~ 6.9 vs L >= 7.6 — but the math must hold everywhere the
# tests can reach.)
_HORIZON_RATIO_FALLBACK = 200.0


def _phi(z: float) -> float:
    """Standard normal CDF (erfc-based — accurate in both tails)."""
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _image_sum(dist: float, L: float, sig_sqrt_t: float) -> float:
    """sum_k sign(dist + 2kL) * 2 * Phi(-|dist + 2kL| / sig_sqrt_t).

    ``dist`` is the barrier distance of the k=0 image (positive for the
    barrier being hit). Terms decay like exp(-2 k^2 L^2 / sig_sqrt_t^2); the
    loop stops the first time both neighbours (k, k+1 and k, k-1) fall
    below 1e-14.
    """
    total = 0.0
    k = 0
    while k <= _K_MAX:
        hk = dist + 2.0 * k * L
        term = 2.0 * _phi(-abs(hk) / sig_sqrt_t)
        if hk < 0:
            term = -term
        total += term
        if k > 0:
            hm = dist - 2.0 * k * L
            termm = 2.0 * _phi(-abs(hm) / sig_sqrt_t)
            if hm < 0:
                termm = -termm
            total += termm
            if term < 1e-14 and termm < 1e-14 and k > 2:
                break
        k += 1
    return total


@dataclass(frozen=True)
class BarrierNull:
    """P(TP first), P(SL first), P(expire) for one bracket, driftless."""

    p_win: float       # pi_W = P(touch +a before -b, before T)
    p_loss: float      # pi_L
    p_expire: float    # pi_E = 1 - pi_W - pi_L
    tp_atr: float      # a  (favourable barrier, ATR units)
    sl_atr: float      # b  (adverse barrier, ATR units)
    t_bars: int        # deadline T in bars
    sigma: float       # per-bar standard deviation, ATR units

    def as_log(self) -> str:
        return (
            f"pi_W={self.p_win:.4f} pi_L={self.p_loss:.4f} "
            f"pi_E={self.p_expire:.4f} tp={self.tp_atr:g}ATR "
            f"sl={self.sl_atr:g}ATR T={self.t_bars} sigma={self.sigma:g}"
        )

    # ── Derived prices (picker conventions) ───────────────────────────

    def ev_atr(self, cost_atr: float = 0.1, m_mtm: float = 0.0) -> float:
        """EV in ATR units, ``net = pnl - cost`` per row convention."""
        return ev_atr(
            self.p_win, self.p_loss, self.p_expire,
            self.tp_atr, self.sl_atr, cost_atr=cost_atr, m_mtm=m_mtm,
        )

    def ruin_odds(self) -> float:
        """No-deadline gambler's-ruin null ``b / (a + b)`` — the anchor the
        measured book is currently BELOW (13-15% TP vs ~37.5% first-touch at
        the shipped bands), i.e. the entry-timing thermometer's zero point."""
        denom = self.tp_atr + self.sl_atr
        return self.sl_atr / denom if denom > 0 else 0.5


def barrier_null(
    tp_atr: float,
    sl_atr: float,
    *,
    t_bars: int = EXPIRY_BARS,
    sigma: float = 1.0,
) -> BarrierNull:
    """Driftless double-barrier first-passage probabilities.

    Validated anchors in tests/test_barrier_null.py; see module docstring
    for the model and units. Raises nothing — degenerate inputs return the
    expired-for-sure null (``pi_E = 1``), the honest answer for a bracket
    that cannot resolve.
    """
    a = float(tp_atr)
    b = float(sl_atr)
    t = int(t_bars)
    sig = float(sigma)
    if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(sig)):
        return BarrierNull(0.0, 0.0, 1.0, a if math.isfinite(a) else 0.0,
                           b if math.isfinite(b) else 0.0, t, sig)
    if a <= 0 or b <= 0 or t <= 0 or sig <= 0:
        return BarrierNull(0.0, 0.0, 1.0, max(a, 0.0), max(b, 0.0), t, max(sig, 0.0))

    L = a + b
    sig_sqrt_t = sig * math.sqrt(t)

    if sig_sqrt_t / L > _HORIZON_RATIO_FALLBACK:
        # Deadline is so distant it is not binding: gambler's ruin.
        pi_w = b / L
        pi_l = a / L
        return BarrierNull(pi_w, pi_l, max(0.0, 1.0 - pi_w - pi_l), a, b, t, sig)

    pi_w = _image_sum(a, L, sig_sqrt_t)
    pi_l = _image_sum(b, L, sig_sqrt_t)  # symmetric: swap a<->b
    total = pi_w + pi_l
    if total > 1.0:  # floating-point overshoot, renormalize (tested)
        pi_w /= total
        pi_l /= total
        total = 1.0
    pi_e = 1.0 - total
    return BarrierNull(pi_w, pi_l, pi_e, a, b, t, sig)


def barrier_null_from_prices(
    entry: float,
    take_profit: float,
    stop_loss: float,
    *,
    atr: float,
    t_bars: int = EXPIRY_BARS,
    sigma: float = 1.0,
    direction: str = "buy",
) -> BarrierNull:
    """Price-space wrapper: log distances, first-order ATR normalization.

    ``a = log(tp/entry) * entry/atr`` (exact to second order for the <15%
    moves this platform trades; the error term is O(distance^2) and below
    0.1% everywhere in the shipped bands). Direction handles the sell side
    (barriers swap roles).
    """
    try:
        e, tp, sl, a_ = float(entry), float(take_profit), float(stop_loss), float(atr)
    except (TypeError, ValueError):
        return BarrierNull(0.0, 0.0, 1.0, 0.0, 0.0, int(t_bars), float(sigma))
    if e <= 0 or a_ <= 0 or tp <= 0 or sl <= 0:
        return BarrierNull(0.0, 0.0, 1.0, 0.0, 0.0, int(t_bars), float(sigma))
    if str(direction).lower().startswith("s"):
        # Sell: TP sits BELOW entry, SL ABOVE. Favourable distance is the
        # downside gap to TP; adverse is the upside gap to SL.
        a = math.log(e / tp) * e / a_ if tp < e else 0.0
        b = math.log(sl / e) * e / a_ if sl > e else 0.0
    else:
        a = math.log(tp / e) * e / a_ if tp > e else 0.0
        b = math.log(e / sl) * e / a_ if sl < e else 0.0
    return barrier_null(a, b, t_bars=t_bars, sigma=sigma)


def ev_atr(
    p_win: float,
    p_loss: float,
    p_expire: float,
    tp_atr: float,
    sl_atr: float,
    *,
    cost_atr: float = 0.1,
    m_mtm: float = 0.0,
) -> float:
    """Three-outcome EV in ATR units — the ONE price.

    Matches ``tiered_atr.walk_envelopes`` (win +tp_atr, loss -sl_atr,
    expire MTM ~ 0) net of ``cost_atr`` charged once per trade, exactly as
    ``select_tp_sl``'s ``net = pnl - cost_atr`` row convention prices it.
    Expire occupies time and pays the cost — it is never charged as a loss.
    """
    return (
        float(p_win) * float(tp_atr)
        - float(p_loss) * float(sl_atr)
        + float(p_expire) * float(m_mtm)
        - float(cost_atr)
    )


def ev_lower_bound_z(q: float = 0.05) -> float:
    """Normal quantile for a one-sided lower bound at mass ``q``.

    Kept here so the gate (posterior.py's ``ev_lower_bound``) and any
    offline replay share ONE definition of "the 5% bound".
    """
    q = min(max(float(q), 1e-6), 0.5)
    if abs(q - 0.05) < 1e-12:
        return _Z_05
    # Inverse normal via bisection on erfc — exact enough for a gate bound.
    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(mid / math.sqrt(2.0)) > q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def solve_geometry_pbm(
    *,
    tp_lo: float,
    tp_hi: float,
    sl_lo: float,
    sl_hi: float,
    min_rr: float,
    cost_atr: float,
    t_bars: int = EXPIRY_BARS,
    sigma: float = 1.0,
    tilt: Callable[[float, float], tuple[float, float, float]] | None = None,
    m_mtm: float = 0.0,
    step: float = 0.25,
) -> tuple[float, float, float, float]:
    """Continuous in-band argmax of the (possibly tilted) barrier EV.

    Phase C's objective: replaces the 9-12 point grid walk with a bounded
    continuous search over the operator band (the band stays as the SEARCH
    DOMAIN, not a set of testable points — proposal §4). ``tilt`` maps a
    candidate ``(a, b)`` to a posterior probability triple; when None the
    pure null prices the search (cold book: physics, not operator prior).

    Deterministic two-stage search: coarse lattice (``step`` ATR), then
    three halving refinements around the incumbent. Worst case ~ 400 EV
    evaluations, each a handful of erfc calls — microseconds against the
    60-path walk it replaces.

    Returns ``(tp_atr, sl_atr, ev_atr, p_expire)``. Degenerate domain
    (empty after ``min_rr``) returns ``(0, 0, -inf, 1)`` — the caller's
    band-centre/no-trade policy decides, never a silently-clamped pick.
    """
    tp_lo, tp_hi = float(tp_lo), float(tp_hi)
    sl_lo, sl_hi = float(sl_lo), float(sl_hi)
    if tp_hi < tp_lo:
        tp_lo, tp_hi = tp_hi, tp_lo
    if sl_hi < sl_lo:
        sl_lo, sl_hi = sl_hi, sl_lo
    min_rr = float(min_rr)
    step = max(float(step), 1e-3)

    def _price(a: float, b: float) -> tuple[float, float]:
        if tilt is None:
            n = barrier_null(a, b, t_bars=t_bars, sigma=sigma)
            return n.ev_atr(cost_atr=cost_atr, m_mtm=m_mtm), n.p_expire
        p_w, p_l, p_e = tilt(a, b)
        return (
            ev_atr(p_w, p_l, p_e, a, b, cost_atr=cost_atr, m_mtm=m_mtm),
            p_e,
        )

    def _rr_ok(a: float, b: float) -> bool:
        return b > 0 and (a / b) >= min_rr - 1e-9

    best: tuple[float, float, float, float] | None = None

    def _consider(a: float, b: float) -> None:
        nonlocal best
        if not (_rr_ok(a, b)):
            return
        ev, p_e = _price(a, b)
        if best is None or ev > best[2]:
            best = (a, b, ev, p_e)

    # Stage 1 — coarse lattice over the band.
    a = tp_lo
    while a <= tp_hi + 1e-9:
        b = sl_lo
        while b <= sl_hi + 1e-9:
            _consider(round(a, 6), round(b, 6))
            b += step
        a += step
    # The band edges themselves are legitimate candidates even when the
    # step does not divide the span evenly.
    for a_edge in (tp_lo, tp_hi):
        for b_edge in (sl_lo, sl_hi):
            _consider(round(a_edge, 6), round(b_edge, 6))

    if best is None:
        return (0.0, 0.0, float("-inf"), 1.0)

    # Stage 2 — halving refinement around the incumbent.
    ref_step = step
    for _ in range(3):
        ref_step *= 0.5
        a0, b0 = best[0], best[1]
        for da in (-ref_step, 0.0, ref_step):
            for db in (-ref_step, 0.0, ref_step):
                a = round(a0 + da, 6)
                b = round(b0 + db, 6)
                if tp_lo - 1e-9 <= a <= tp_hi + 1e-9 and sl_lo - 1e-9 <= b <= sl_hi + 1e-9:
                    _consider(a, b)

    assert best is not None
    return best
