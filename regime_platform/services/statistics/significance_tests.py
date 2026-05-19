"""
Strategy Significance Tests (Phase 6D).

Implements two rigorous multiple-strategy significance tests:

  1. White's Reality Check (WRC) — tests whether the best strategy from
     a set beats a benchmark by more than chance, using bootstrap
     resampling of returns.

  2. Hansen's SPA (Superior Predictive Ability) — improves on WRC by
     using a studentized test statistic and a recentering approach
     that yields more accurate size and power.

Both tests address the fundamental question: "Is the best strategy in
my optimization sweep genuinely better than a benchmark, or did it just
get lucky because I tested many strategies?"

The null hypothesis for both tests is that NO strategy beats the
benchmark. Rejection means at least one strategy is genuinely superior.

References:
  - White, H. (2000). "A Reality Check for Data Snooping", Econometrica
  - Hansen, P.R. (2005). "A Test for Superior Predictive Ability",
    Journal of Business & Economic Statistics
  - Politis, D.N. & Romano, J.P. (1994). "The Stationary Bootstrap", JASA
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class SignificanceTestResult:
    """Result of a strategy significance test (WRC or SPA)."""

    test_name: str                # "whites_reality_check" or "hansen_spa"
    p_value: float                # Bootstrap p-value
    test_statistic: float         # Observed test statistic
    n_strategies: int             # Number of strategies tested
    n_bootstrap: int              # Number of bootstrap resamples
    n_observations: int           # Number of return observations per strategy
    is_significant: bool          # p_value < alpha
    best_strategy_index: int      # Index of the best strategy
    best_strategy_sharpe: float   # Sharpe of the best strategy
    benchmark_sharpe: float       # Sharpe of the benchmark (0.0 if zero benchmark)
    interpretation: str           # Human-readable interpretation

    def to_dict(self) -> dict:
        return {
            "test_name": self.test_name,
            "p_value": round(self.p_value, 6),
            "test_statistic": round(self.test_statistic, 6),
            "n_strategies": self.n_strategies,
            "n_bootstrap": self.n_bootstrap,
            "n_observations": self.n_observations,
            "is_significant": self.is_significant,
            "best_strategy_index": self.best_strategy_index,
            "best_strategy_sharpe": round(self.best_strategy_sharpe, 6),
            "benchmark_sharpe": round(self.benchmark_sharpe, 6),
            "interpretation": self.interpretation,
        }


# ── White's Reality Check ───────────────────────────────────────────


def whites_reality_check(
    strategy_returns: Sequence[np.ndarray | Sequence[float]],
    benchmark_returns: np.ndarray | Sequence[float] | None = None,
    n_bootstrap: int = 1000,
    alpha: float = 0.05,
    block_size: int | None = None,
    seed: int | None = None,
) -> SignificanceTestResult:
    """
    White's Reality Check (WRC) — test whether any strategy beats a
    benchmark after accounting for data snooping.

    Algorithm:
      1. Compute relative performance d_t,i = r_t,i - r_t,bm for each
         strategy i against the benchmark at each time t.
      2. Compute the test statistic: V = max_i(sqrt(T) * mean(d_i))
      3. Bootstrap the returns series (stationary bootstrap) and
         recompute V* for each resample.
      4. P-value = fraction of V* >= V_observed.

    Parameters
    ----------
    strategy_returns : list of array-like
        Each element is a return series for one strategy. All series
        must have the same length T.
    benchmark_returns : array-like, optional
        Benchmark returns (same length T). Default: zero (tests whether
        any strategy has positive Sharpe).
    n_bootstrap : int
        Number of bootstrap resamples (default 1000).
    alpha : float
        Significance level (default 0.05).
    block_size : int, optional
        Block size for stationary bootstrap. Default: T^(1/3).
    seed : int, optional
        Random seed for reproducibility.

    Returns
    -------
    SignificanceTestResult with WRC p-value and metadata.
    """
    # Convert to numpy arrays
    strategies = [np.asarray(s, dtype=float) for s in strategy_returns]
    n_strategies = len(strategies)

    if n_strategies == 0:
        return _trivial_result("whites_reality_check", 0)

    T = len(strategies[0])

    # Validate same length
    for i, s in enumerate(strategies):
        if len(s) != T:
            raise ValueError(
                f"Strategy {i} has length {len(s)}, expected {T}. "
                f"All strategy return series must have the same length."
            )

    if T < 10:
        return _trivial_result("whites_reality_check", n_strategies, T)

    # Default benchmark: zero returns (test if any strategy has positive mean)
    if benchmark_returns is not None:
        bm = np.asarray(benchmark_returns, dtype=float)
        if len(bm) != T:
            raise ValueError(
                f"Benchmark has length {len(bm)}, expected {T}."
            )
    else:
        bm = np.zeros(T)

    # Compute performance differences: d_t,i = r_t,i - r_t,bm
    D = np.column_stack([s - bm for s in strategies])  # (T, n_strategies)

    # Compute mean differences
    d_bar = np.mean(D, axis=0)  # (n_strategies,)

    # Test statistic: V = max_i(sqrt(T) * d_bar_i)
    V_observed = float(np.max(np.sqrt(T) * d_bar))

    # Best strategy
    best_idx = int(np.argmax(d_bar))
    best_sharpe = float(np.mean(strategies[best_idx]) / np.std(strategies[best_idx], ddof=1)) \
        if np.std(strategies[best_idx], ddof=1) > 0 else 0.0
    bm_sharpe = float(np.mean(bm) / np.std(bm, ddof=1)) if len(bm) > 1 and np.std(bm, ddof=1) > 0 else 0.0

    # Stationary bootstrap (Politis & Romano)
    if block_size is None:
        block_size = max(2, int(np.ceil(T ** (1.0 / 3.0))))

    rng = np.random.default_rng(seed)

    # Circular data for block bootstrap
    D_circular = np.vstack([D, D[:block_size]])

    V_bootstrap = np.empty(n_bootstrap)

    for b in range(n_bootstrap):
        # Generate bootstrap sample using stationary bootstrap
        sample_indices = []
        current_length = 0

        while current_length < T:
            start = rng.integers(0, T)
            end = start + block_size
            indices = list(range(start, min(end, T + block_size)))
            sample_indices.extend(indices)
            current_length += len(indices)

        sample_indices = sample_indices[:T]

        # Compute mean of bootstrapped differences
        D_boot = D_circular[sample_indices]
        d_bar_boot = np.mean(D_boot, axis=0)

        # Recentered test statistic (White's approach)
        V_bootstrap[b] = float(np.max(np.sqrt(T) * (d_bar_boot - d_bar)))

    # P-value: fraction of bootstrap V >= observed V
    p_value = float(np.mean(V_bootstrap >= V_observed))

    is_significant = p_value < alpha

    interpretation = _build_interpretation(
        test_name="White's Reality Check",
        p_value=p_value,
        n_strategies=n_strategies,
        is_significant=is_significant,
        alpha=alpha,
    )

    return SignificanceTestResult(
        test_name="whites_reality_check",
        p_value=p_value,
        test_statistic=V_observed,
        n_strategies=n_strategies,
        n_bootstrap=n_bootstrap,
        n_observations=T,
        is_significant=is_significant,
        best_strategy_index=best_idx,
        best_strategy_sharpe=best_sharpe,
        benchmark_sharpe=bm_sharpe,
        interpretation=interpretation,
    )


# ── Hansen's Superior Predictive Ability (SPA) ─────────────────────


def hansen_spa(
    strategy_returns: Sequence[np.ndarray | Sequence[float]],
    benchmark_returns: np.ndarray | Sequence[float] | None = None,
    n_bootstrap: int = 1000,
    alpha: float = 0.05,
    block_size: int | None = None,
    seed: int | None = None,
) -> SignificanceTestResult:
    """
    Hansen's Superior Predictive Ability (SPA) test.

    Improves on White's Reality Check by:
      1. Using studentized test statistics (dividing by standard error)
         for better size control
      2. Recentering: strategies with clearly negative performance are
         recentered to zero, improving power against the null

    Algorithm:
      1. Compute d_t,i = r_t,i - r_t,bm for each strategy
      2. Studentize: t_i = d_bar_i / se(d_bar_i)
      3. SPA statistic: SPA = max_i(t_i)
      4. Recenter: strategies where d_bar_i < -c * se(d_bar_i) are
         recentered to zero (c ≈ 2, roughly 2 standard errors)
      5. Bootstrap under null and compute p-value

    Parameters
    ----------
    strategy_returns : list of array-like
        Return series for each strategy (all same length T).
    benchmark_returns : array-like, optional
        Benchmark returns (default: zeros).
    n_bootstrap : int
        Number of bootstrap resamples (default 1000).
    alpha : float
        Significance level (default 0.05).
    block_size : int, optional
        Block size for stationary bootstrap. Default: T^(1/3).
    seed : int, optional
        Random seed for reproducibility.

    Returns
    -------
    SignificanceTestResult with SPA p-value and metadata.
    """
    # Convert to numpy arrays
    strategies = [np.asarray(s, dtype=float) for s in strategy_returns]
    n_strategies = len(strategies)

    if n_strategies == 0:
        return _trivial_result("hansen_spa", 0)

    T = len(strategies[0])

    for i, s in enumerate(strategies):
        if len(s) != T:
            raise ValueError(
                f"Strategy {i} has length {len(s)}, expected {T}."
            )

    if T < 10:
        return _trivial_result("hansen_spa", n_strategies, T)

    # Default benchmark: zero
    if benchmark_returns is not None:
        bm = np.asarray(benchmark_returns, dtype=float)
        if len(bm) != T:
            raise ValueError(
                f"Benchmark has length {len(bm)}, expected {T}."
            )
    else:
        bm = np.zeros(T)

    # Compute performance differences
    D = np.column_stack([s - bm for s in strategies])  # (T, n_strategies)

    # Mean and standard errors of differences
    d_bar = np.mean(D, axis=0)  # (n_strategies,)
    d_se = np.std(D, axis=0, ddof=1) / np.sqrt(T)  # (n_strategies,)

    # Studentized test statistics
    # Avoid division by zero
    d_se_safe = np.where(d_se < 1e-10, 1e-10, d_se)
    t_stats = d_bar / d_se_safe

    # SPA test statistic: max of studentized statistics
    SPA_observed = float(np.max(t_stats))

    # Best strategy
    best_idx = int(np.argmax(d_bar))
    best_sharpe = float(np.mean(strategies[best_idx]) / np.std(strategies[best_idx], ddof=1)) \
        if np.std(strategies[best_idx], ddof=1) > 0 else 0.0
    bm_sharpe = float(np.mean(bm) / np.std(bm, ddof=1)) if len(bm) > 1 and np.std(bm, ddof=1) > 0 else 0.0

    # Hansen's recentering: strategies that are clearly underperforming
    # (more than c standard errors below zero) are recentered to zero
    # This improves power by not penalizing for clearly inferior strategies
    c = 2.0  # Recentering threshold (approximately 2 SEs)
    recentered_d_bar = d_bar.copy()
    for i in range(n_strategies):
        if d_bar[i] < -c * d_se_safe[i]:
            recentered_d_bar[i] = 0.0

    # Stationary bootstrap
    if block_size is None:
        block_size = max(2, int(np.ceil(T ** (1.0 / 3.0))))

    rng = np.random.default_rng(seed)
    D_circular = np.vstack([D, D[:block_size]])

    SPA_bootstrap = np.empty(n_bootstrap)

    for b in range(n_bootstrap):
        # Block bootstrap sampling
        sample_indices = []
        current_length = 0

        while current_length < T:
            start = rng.integers(0, T)
            end = start + block_size
            indices = list(range(start, min(end, T + block_size)))
            sample_indices.extend(indices)
            current_length += len(indices)

        sample_indices = sample_indices[:T]
        D_boot = D_circular[sample_indices]

        # Compute bootstrap mean differences
        d_bar_boot = np.mean(D_boot, axis=0)

        # Recentered bootstrap statistics
        # Subtract the recentered mean and add 0 (or keep original for positive d_bar)
        d_boot_centered = d_bar_boot - recentered_d_bar

        # Studentize bootstrap statistics
        d_se_boot = np.std(D_boot, axis=0, ddof=1) / np.sqrt(T)
        d_se_boot_safe = np.where(d_se_boot < 1e-10, 1e-10, d_se_boot)

        SPA_bootstrap[b] = float(np.max(d_boot_centered / d_se_boot_safe))

    # P-value
    p_value = float(np.mean(SPA_bootstrap >= SPA_observed))

    is_significant = p_value < alpha

    interpretation = _build_interpretation(
        test_name="Hansen's SPA",
        p_value=p_value,
        n_strategies=n_strategies,
        is_significant=is_significant,
        alpha=alpha,
    )

    return SignificanceTestResult(
        test_name="hansen_spa",
        p_value=p_value,
        test_statistic=SPA_observed,
        n_strategies=n_strategies,
        n_bootstrap=n_bootstrap,
        n_observations=T,
        is_significant=is_significant,
        best_strategy_index=best_idx,
        best_strategy_sharpe=best_sharpe,
        benchmark_sharpe=bm_sharpe,
        interpretation=interpretation,
    )


# ── Internal helpers ────────────────────────────────────────────────


def _trivial_result(
    test_name: str,
    n_strategies: int,
    n_obs: int = 0,
) -> SignificanceTestResult:
    """Return a trivial result for edge cases."""
    return SignificanceTestResult(
        test_name=test_name,
        p_value=1.0,
        test_statistic=0.0,
        n_strategies=n_strategies,
        n_bootstrap=0,
        n_observations=n_obs,
        is_significant=False,
        best_strategy_index=0,
        best_strategy_sharpe=0.0,
        benchmark_sharpe=0.0,
        interpretation=f"Insufficient data for {test_name} test (need at least 10 observations per strategy).",
    )


def _build_interpretation(
    test_name: str,
    p_value: float,
    n_strategies: int,
    is_significant: bool,
    alpha: float,
) -> str:
    """Build a human-readable interpretation of the test result."""
    if is_significant:
        return (
            f"{test_name} p-value = {p_value:.4f} < {alpha}: REJECT the null hypothesis. "
            f"At least one of the {n_strategies} strategies shows genuinely superior "
            f"performance after accounting for data snooping bias. The best strategy's "
            f"performance is unlikely to be a multiple-testing artifact."
        )
    else:
        return (
            f"{test_name} p-value = {p_value:.4f} >= {alpha}: FAIL TO REJECT the null. "
            f"None of the {n_strategies} strategies demonstrate statistically significant "
            f"superiority after accounting for data snooping. The observed performance "
            f"differences could plausibly arise from testing multiple strategies on the "
            f"same data. Consider reducing the parameter space, increasing sample size, "
            f"or validating out-of-sample."
        )
