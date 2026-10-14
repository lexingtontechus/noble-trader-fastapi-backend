"""posterior.py — the empirical layer's invariants (conjugacy, decay,
shrinkage, the gate, the sizing) and the resolver-hook semantics that close
survivorship by construction.

Pure-math tests run without a database. The I/O surface (read_cell /
update_cell / record_pbm_shadow / rebuild) is contract-tested against a
stubbed httpx transport so the fetch-then-upsert shape is pinned without
network.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.barrier import barrier_null
from regime_platform.sweep_orchestrator.posterior import (
    CellPosterior,
    bucket_tilt,
    conservative_kelly,
    deadline_class_of,
    decay_lambda,
    dirichlet_moments,
    dirichlet_p_w_sd,
    ev_lower_bound,
    ev_moments,
)

# ─── Conjugacy and moments ──────────────────────────────────────────────────


def test_dirichlet_moments_normalize():
    for alpha in [(10.0, 5.0, 3.0), (0.1, 0.2, 0.3), (100.0, 1.0, 1.0)]:
        means, a0 = dirichlet_moments(alpha)
        assert sum(means) == pytest.approx(1.0, abs=1e-12)
        assert a0 == pytest.approx(sum(alpha))
    means, a0 = dirichlet_moments((0.0, 0.0, 0.0))
    assert means == (1 / 3, 1 / 3, 1 / 3)  # degenerate: uniform
    assert a0 == 0.0


def test_k0_anchor_is_physics_for_cold_cell():
    """A cell with ZERO evidence must price exactly at the analytic null —
    the no-cliff property (cold book = physics, not 0.5, not None)."""
    null = barrier_null(3.0, 1.5)
    cell = CellPosterior(symbol="X", side="buy", vol_tier="med_vol",
                         bucket="2-4", deadline_class="standard", era="live")
    means = cell.means(null)
    # k0 default may vary; p vector must EQUAL the null when s == 0
    assert means[0] == pytest.approx(null.p_win, abs=1e-12)
    assert means[1] == pytest.approx(null.p_loss, abs=1e-12)
    assert means[2] == pytest.approx(null.p_expire, abs=1e-12)


def test_evidence_moves_posterior_off_null_monotonically():
    """More wins -> higher p_W, proportionally (conjugate update, no cliffs)."""
    null = barrier_null(3.0, 1.5)
    prev = -1.0
    for s_w in (0.0, 5.0, 20.0, 100.0, 1000.0):
        cell = CellPosterior(symbol="X", side="buy", vol_tier="v",
                             bucket="2-4", deadline_class="standard",
                             era="live", s_w=s_w, s_l=5.0, s_e=1.0)
        p_w = cell.means(null)[0]
        assert p_w > prev
        prev = p_w
    # heavy evidence: empirical share dominates the anchor (the residual
    # pull is k0/(k0 + n_eff) ~ 3% at n_eff=1006)
    cell = CellPosterior(symbol="X", side="buy", vol_tier="v",
                         bucket="2-4", deadline_class="standard",
                         era="live", s_w=1000.0, s_l=5.0, s_e=1.0)
    assert cell.means(null)[0] == pytest.approx(1000.0 / 1006.0, abs=0.03)


def test_gap_signs():
    null = barrier_null(3.0, 1.5)
    dead = CellPosterior(symbol="X", side="buy", vol_tier="v", bucket="2-4",
                         deadline_class="standard", era="live",
                         s_w=2.0, s_l=40.0, s_e=8.0)
    hot = CellPosterior(symbol="X", side="buy", vol_tier="v", bucket="2-4",
                        deadline_class="standard", era="live",
                        s_w=40.0, s_l=2.0, s_e=8.0)
    assert dead.gap(null) < 0   # adverse-timed (the platform's measured state)
    assert hot.gap(null) > 0    # genuine timing skill
    assert CellPosterior(symbol="X", side="buy", vol_tier="v", bucket="2-4",
                         deadline_class="standard", era="live").gap(null) == 0.0


# ─── Decay ──────────────────────────────────────────────────────────────────


def test_decay_lambda_halflife():
    tau = 14.0
    assert decay_lambda(0.0, tau) == 1.0
    assert decay_lambda(tau, tau) == pytest.approx(0.5)
    assert decay_lambda(2 * tau, tau) == pytest.approx(0.25)
    assert decay_lambda(-5.0, tau) == 1.0  # future rows keep full weight


# ─── The one gate: EV lower bound ───────────────────────────────────────────


def test_ev_bound_tightens_with_evidence():
    """Thin evidence -> wide interval -> bound far below the mean; heavy
    evidence -> bound approaches its own mean. This is the property that
    retires min_signals/min_fold_n proxies: the interval IS the sample
    floor, continuously."""
    null = barrier_null(3.0, 1.5)
    tp, sl, cost = 3.0, 1.5, 0.1
    bounds, means, gaps = [], [], []
    for s_w in (0.0, 10.0, 100.0, 2000.0):
        cell = CellPosterior(symbol="X", side="buy", vol_tier="v", bucket="2-4",
                             deadline_class="standard", era="live",
                             s_w=s_w + 10.0, s_l=10.0, s_e=2.0)
        alpha = cell.anchor(null)
        mean_ev, _ = ev_moments(alpha, tp, sl, cost_atr=cost)
        bounds.append(ev_lower_bound(alpha, tp, sl, cost_atr=cost))
        means.append(mean_ev)
        gaps.append(mean_ev - bounds[-1])
    # bound never above its own mean; monotone in evidence; uncertainty gap
    # (mean - bound) shrinks monotonically as the posterior concentrates
    assert all(b <= m + 1e-9 for b, m in zip(bounds, means))
    assert bounds == sorted(bounds)
    assert gaps == sorted(gaps, reverse=True)
    assert bounds[-1] > bounds[0]


def test_ev_moments_match_direct_expectation():
    alpha = (12.0, 6.0, 6.0)
    tp, sl, cost, m = 3.0, 1.0, 0.1, 0.05
    means, _ = dirichlet_moments(alpha)
    direct = means[0] * tp - means[1] * sl + means[2] * m - cost
    got, sd = ev_moments(alpha, tp, sl, cost_atr=cost, m_mtm=m)
    assert got == pytest.approx(direct, abs=1e-12)
    assert sd >= 0.0
    # alpha -> huge: sd -> 0 (posterior concentrates)
    _, sd_big = ev_moments((1e9, 5e8, 5e8), tp, sl, cost_atr=cost)
    assert sd_big < sd * 1e-3


def test_dirichlet_p_w_sd_bounds():
    assert dirichlet_p_w_sd((1.0, 1.0, 1.0)) <= 0.5
    # sd(1000,500,500) = sqrt(1000*500/(2000^2*2001)) ~= 0.0112 — concentrates
    # as O(1/sqrt(a0)); pin it well below the thin-cell sd
    thin = dirichlet_p_w_sd((1.0, 1.0, 1.0))
    thick = dirichlet_p_w_sd((1000.0, 500.0, 500.0))
    assert thick < thin * 0.1


# ─── Conservative Kelly (Phase D) ───────────────────────────────────────────


def test_conservative_kelly_monotone_in_evidence():
    k = 0.10
    # bound collapses -> size collapses; bound = mean -> full size
    assert conservative_kelly(k, 0.5, -1.0) == 0.0
    assert conservative_kelly(k, 0.5, 0.0) == 0.0
    assert conservative_kelly(k, 0.5, 0.25) == pytest.approx(k * 0.5)
    assert conservative_kelly(k, 0.5, 0.5) == pytest.approx(k)
    assert conservative_kelly(k, 0.5, 0.9) == pytest.approx(k)  # capped, never above
    # negative EV never sizes
    assert conservative_kelly(k, -0.2, -0.1) == 0.0
    assert conservative_kelly(k, 0.0, 0.0) == 0.0


# ─── Deadline classing ──────────────────────────────────────────────────────


def test_deadline_class_mapping():
    assert deadline_class_of({}) == "standard"
    assert deadline_class_of({"soft_deadline_hours": None}) == "standard"
    assert deadline_class_of({"soft_deadline_hours": 0.5}) == "short"
    assert deadline_class_of({"soft_deadline_hours": 30.0}) == "medium"
    assert deadline_class_of({"soft_deadline_hours": 120.0}) == "long"
    assert deadline_class_of({"soft_deadline_hours": "garbage"}) == "standard"
    assert deadline_class_of({"hard_deadline_hours": 90.0}) == "long"


# ─── The geometry tilt (Phase C) ────────────────────────────────────────────


def _cell(bucket: str, s_w: float, s_l: float, s_e: float, m: float = 0.0) -> CellPosterior:
    return CellPosterior(symbol="X", side="buy", vol_tier="v", bucket=bucket,
                         deadline_class="standard", era="live",
                         s_w=s_w, s_l=s_l, s_e=s_e, m_mtm=m)


def test_bucket_tilt_cold_book_is_the_null():
    tilt = bucket_tilt([])
    for a, b in [(2.0, 1.0), (4.0, 2.0)]:
        n = barrier_null(a, b)
        assert tilt(a, b) == pytest.approx((n.p_win, n.p_loss, n.p_expire))


def test_bucket_tilt_dead_bucket_suppresses_wins():
    """0 wins in n resolved is the strongest measured signal — it must
    actually bite (the regression this pins: an ``or`` that flattened a
    legitimate 0.0 f_raw to 'no evidence')."""
    cells = [_cell("2-4", 0.0, 60.0, 10.0)]
    tilt = bucket_tilt(cells)
    a, b = 3.0, 1.5
    n = barrier_null(a, b)
    p_w, p_l, p_e = tilt(a, b)
    assert p_w < n.p_win  # heavily suppressed, not equal
    assert p_w + p_l + p_e == pytest.approx(1.0, abs=1e-9)
    assert p_e >= 0.0 and p_w >= 0.0 and p_l >= 0.0


def test_bucket_tilt_normalizes_and_buckets_by_tp():
    cells = [_cell("2-4", 30.0, 10.0, 5.0), _cell("4+", 1.0, 1.0, 1.0)]
    tilt = bucket_tilt(cells)
    for a, b in [(3.0, 1.5), (5.0, 2.0), (1.2, 0.8)]:
        p_w, p_l, p_e = tilt(a, b)
        assert p_w + p_l + p_e == pytest.approx(1.0, abs=1e-9)
        assert 0.0 <= p_w <= 1.0 and 0.0 <= p_l <= 1.0 and 0.0 <= p_e <= 1.0
    # the measured-hot bucket (2-4: 30W/10L) tilts wins UP vs the null;
    # the thin bucket (4+) barely moves
    n24 = barrier_null(3.0, 1.5)
    assert tilt(3.0, 1.5)[0] > n24.p_win
    n4 = barrier_null(5.0, 2.0)
    assert abs(tilt(5.0, 2.0)[0] - n4.p_win) < abs(tilt(3.0, 1.5)[0] - n24.p_win)


def test_bucket_tilt_skips_legacy_era_cells():
    legacy = CellPosterior(symbol="X", side="buy", vol_tier="v", bucket="2-4",
                           deadline_class="standard", era="legacy",
                           s_w=100.0, s_l=0.0, s_e=0.0)
    tilt = bucket_tilt([legacy])
    n = barrier_null(3.0, 1.5)
    assert tilt(3.0, 1.5) == pytest.approx((n.p_win, n.p_loss, n.p_expire))


# ─── Regression: the platform's measured adverse timing prices below the
# ─── gate under honest evidence ─────────────────────────────────────────────


def test_adverse_timed_cell_fails_the_gate():
    """The book's measured state (win rate BELOW the random-walk null) must
    fail the one gate on honest evidence — the methodology's honest default
    is no-trade, and the gate says so without any bolt-on floor."""
    null = barrier_null(4.94, 1.0)  # a shipped-band geometry
    cell = CellPosterior(symbol="XAUUSD", side="sell", vol_tier="med_vol",
                         bucket="4+", deadline_class="standard", era="live",
                         s_w=6.0, s_l=34.0, s_e=10.0)  # 15% realized vs ~39% null region
    alpha = cell.anchor(null)
    lb = ev_lower_bound(alpha, 4.94, 1.0, cost_atr=0.1)
    assert lb < 0.0
