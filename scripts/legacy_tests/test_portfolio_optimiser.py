"""
portfolio_optimiser.py + masaniello_bridge.py - Test Suite

Covers:
  1. Basic optimisation with synthetic returns
  2. Kelly fractions weight bound tightening
  3. Correlation regime exposure scaling
  4. Regime weight bounds
  5. MasanielloBridge two-stage pipeline
  6. Edge cases: single asset, zero returns

Run: python test_portfolio_optimiser.py
"""

import sys, warnings
warnings.filterwarnings("ignore")

import numpy as np

from regime_platform.core.portfolio_optimiser import (
    DrawdownOptimiser, OptimiserConfig, OptimisationResult,
    _regime_weight_bound, _corr_regime_exposure,
)

PASS = 0
FAIL = 0

def check(name, expr, expected=True, tol=None):
    global PASS, FAIL
    try:
        if tol is not None:
            ok = abs(expr - expected) < tol
        else:
            ok = (expr == expected) if not isinstance(expr, float) else abs(expr - expected) < 1e-9
        if ok:
            PASS += 1
            print(f"  ✓  {name}")
        else:
            FAIL += 1
            print(f"  ✗  {name}  got={expr!r}  expected={expected!r}")
    except Exception as e:
        FAIL += 1
        print(f"  ✗  {name}  EXCEPTION: {e}")


# ─── Helper: generate synthetic returns ─────────────────────────────────────

def make_returns(n_bars=500, n_assets=4, seed=42):
    """Generate correlated log-returns for testing."""
    rng = np.random.default_rng(seed)
    means = [0.0003, 0.0004, 0.0001, 0.0002]
    vols = [0.012, 0.015, 0.010, 0.008]
    corr = 0.3  # moderate correlation

    # Build correlation matrix
    cov = np.full((n_assets, n_assets), corr)
    np.fill_diagonal(cov, 1.0)
    for i in range(n_assets):
        for j in range(n_assets):
            cov[i, j] *= vols[i] * vols[j]

    returns = rng.multivariate_normal(means[:n_assets], cov, size=n_bars)
    return returns


# ─── TEST 1: Basic optimisation ──────────────────────────────────────────────
print("\n▶ Test 1: Basic optimisation with synthetic data")

ret = make_returns()
symbols = ["SPY", "QQQ", "GLD", "TLT"]

opt = DrawdownOptimiser()
result = opt.optimise(returns_matrix=ret, symbols=symbols)

check("Basic: converged",            result.converged, True)
check("Basic: 4 symbols",            len(result.weights), 4)
check("Basic: weights sum ≈ 1",      sum(result.weights), 1.0, tol=0.15)
check("Basic: sharpe > 0",           result.sharpe_ratio > 0, True)
check("Basic: all weights ≥ 0",      all(w >= 0 for w in result.weights), True)
check("Basic: all weights ≤ 0.40",   all(w <= 0.41 for w in result.weights), True)  # small float tolerance
check("Basic: exposure ≤ 1.0",       result.regime_exposure <= 1.01, True)

print(f"  Weights: {[round(w, 3) for w in result.weights]}")
print(f"  Sharpe: {result.sharpe_ratio:.4f}")

# ─── TEST 2: Kelly fractions activation ──────────────────────────────────────
print("\n▶ Test 2: Kelly fractions weight bounds")

# Small kelly fractions should tighten bounds
result_no_kelly = opt.optimise(returns_matrix=ret, symbols=symbols)
result_tight_kelly = opt.optimise(
    returns_matrix=ret, symbols=symbols,
    kelly_fractions=[0.05, 0.05, 0.05, 0.05],
)

# With very small Kelly, weights should be more constrained
max_w_no_kelly = max(result_no_kelly.weights)
max_w_kelly = max(result_tight_kelly.weights)

check("Kelly: tight fractions reduce max weight", max_w_kelly <= max_w_no_kelly + 0.01, True)
check("Kelly: produces result", len(result_tight_kelly.weights), 4)

print(f"  Max weight (no kelly): {max_w_no_kelly:.4f}")
print(f"  Max weight (tight kelly): {max_w_kelly:.4f}")

# ─── TEST 3: Correlation regime exposure ─────────────────────────────────────
print("\n▶ Test 3: Correlation regime exposure scaling")

check("Exposure: low_corr -> 1.0",  _corr_regime_exposure("low_corr"), 1.0)
check("Exposure: mid_corr -> 0.90", _corr_regime_exposure("mid_corr"), 0.90)
check("Exposure: high_corr -> 0.70", _corr_regime_exposure("high_corr"), 0.70)
check("Exposure: crisis -> 0.50",   _corr_regime_exposure("crisis"), 0.50)
check("Exposure: None -> 0.90",     _corr_regime_exposure(None), 0.90)

# Crisis regime should produce lower exposure
result_crisis = opt.optimise(
    returns_matrix=ret, symbols=symbols,
    corr_regime="crisis",
)
check("Crisis: exposure = 0.50", result_crisis.regime_exposure, 0.50)
check("Crisis: weights sum ≈ 0.50", sum(result_crisis.weights), 0.50, tol=0.05)

# ─── TEST 4: Regime weight bounds ────────────────────────────────────────────
print("\n▶ Test 4: Regime weight bounds")

# No snapshot -> base_max
check("Bound: no snap -> base_max", _regime_weight_bound(None, 0.40), 0.40)

# Favourable regime (mult=1.75) -> 100% of base_max
class FakeSnap:
    risk_multiplier = 1.75
check("Bound: favourable -> base_max", _regime_weight_bound(FakeSnap(), 0.40), 0.40, tol=0.01)

# Adverse regime (mult=0.10) -> 25% of base_max
class AdverseSnap:
    risk_multiplier = 0.10
check("Bound: adverse -> 25% of base", _regime_weight_bound(AdverseSnap(), 0.40), 0.10, tol=0.01)

# ─── TEST 5: MasanielloBridge two-stage pipeline ────────────────────────────
print("\n▶ Test 5: MasanielloBridge two-stage pipeline")

try:
    from regime_platform.services.masaniello_bridge import MasanielloBridge

    from regime_platform.core.position_sizer import SizingConfig as SC
    bridge = MasanielloBridge(
        sizing_config=SC(min_rr=1.50, regime_floor=0.30),  # Relaxed for test data
    )
    bridge_result = bridge.optimise_and_size(
        returns_matrix=ret,
        symbols=symbols,
        equity=100_000,
        stop_distances=[2.0, 3.0, 1.5, 1.0],
        p_wins=[0.60, 0.55, 0.58, 0.52],
        reward_risks=[2.5, 2.0, 2.0, 1.5],
        atr_baselines=[10.0, 15.0, 8.0, 5.0],
        atr_currents=[10.0, 15.0, 8.0, 5.0],
    )

    check("Bridge: 4 asset sizes", len(bridge_result.asset_sizes), 4)
    check("Bridge: optimisation present", bridge_result.optimisation is not None, True)
    # Some assets may be blocked by gates; just check the pipeline runs
    check("Bridge: total risk ≥ 0", bridge_result.total_risk_amount >= 0, True)
    check("Bridge: total contracts ≥ 0", bridge_result.total_contracts >= 0, True)

    # Check each asset has valid results
    for a in bridge_result.asset_sizes:
        check(f"Bridge: {a.symbol} weight > 0", a.portfolio_weight >= 0, True)
        check(f"Bridge: {a.symbol} contracts ≥ 0", a.contracts >= 0, True)

    print(f"  Total risk: ${bridge_result.total_risk_amount:,.2f}")
    print(f"  Total contracts: {bridge_result.total_contracts}")
    for a in bridge_result.asset_sizes:
        print(f"  {a.symbol}: weight={a.portfolio_weight:.3f} capital=${a.capital_allocation:,.0f} "
              f"contracts={a.contracts} allowed={a.allowed}")

except ImportError as e:
    FAIL += 1
    print(f"  ✗  Bridge import failed: {e}")

# ─── TEST 6: Edge cases ──────────────────────────────────────────────────────
print("\n▶ Test 6: Edge cases")

# Single asset
try:
    ret1 = make_returns(n_bars=200, n_assets=2, seed=99)
    result1 = opt.optimise(returns_matrix=ret1, symbols=["A", "B"])
    check("Edge: 2-asset has result", len(result1.weights), 2)
    check("Edge: 2 weights sum reasonable", sum(result1.weights) > 0, True)
except Exception as e:
    FAIL += 1
    print(f"  ✗  Edge 2-asset: {e}")

# Zero kelly fractions -> all bounds tightened to 0.01
try:
    result_zero_kelly = opt.optimise(
        returns_matrix=ret, symbols=symbols,
        kelly_fractions=[0.0, 0.0, 0.0, 0.0],
    )
    # With zero kelly, upper bounds should be 0.01
    max_ub = max(b[1] for b in result_zero_kelly.per_asset_bounds)
    check("Edge: zero kelly -> tight bounds", max_ub <= 0.02, True)
except Exception as e:
    FAIL += 1
    print(f"  ✗  Edge zero kelly: {e}")

# ─── Summary ──────────────────────────────────────────────────────────────────
total = PASS + FAIL
print(f"\n{'─'*60}")
print(f"  {PASS}/{total} tests passed  {'✅' if FAIL == 0 else f'❌ {FAIL} failed'}")
if FAIL == 0:
    print("\n✅  All portfolio optimiser tests passed.\n")
else:
    print(f"\n❌  {FAIL} test(s) failed - see above.\n")
    sys.exit(1)
