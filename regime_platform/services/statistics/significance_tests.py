"""
Strategy Significance Tests — White's Reality Check and Hansen's SPA.

These tests answer the fundamental question: "Is the best strategy from 
a set of N strategies genuinely better than a benchmark, or is it just 
the best of N random walks?"

References:
  - White (2000) "A Reality Check for Data Snooping"
  - Hansen (2005) "A Test for Superior Predictive Ability"
"""

import numpy as np
from typing import Dict, Any, Optional, List
from scipy import stats as sp_stats


def _compute_bootstrap_statistic(
    excess_returns: np.ndarray,
    n_bootstrap: int = 1000,
    method: str = "white",
) -> np.ndarray:
    """
    Compute bootstrap distribution of the test statistic.
    
    For White's RC: V_bar = max over strategies of mean(excess returns)
    For Hansen's SPA: uses studentized statistics and a different null centering.
    """
    n_strategies, n_periods = excess_returns.shape
    
    bootstrap_stats = np.empty(n_bootstrap)
    
    for b in range(n_bootstrap):
        # Resample time periods with replacement
        indices = np.random.choice(n_periods, size=n_periods, replace=True)
        resampled = excess_returns[:, indices]
        
        if method == "white":
            # White: max of mean excess returns
            means = np.mean(resampled, axis=1)
            bootstrap_stats[b] = np.max(means)
        else:  # hansen
            # Hansen SPA: studentized max
            means = np.mean(resampled, axis=1)
            stds = np.std(resampled, axis=1, ddof=1)
            stds = np.where(stds > 0, stds, 1e-10)
            studentized = means / (stds / np.sqrt(n_periods))
            bootstrap_stats[b] = np.max(studentized)
    
    return bootstrap_stats


def whites_reality_check(
    strategy_returns: np.ndarray,
    benchmark_returns: Optional[np.ndarray] = None,
    n_bootstrap: int = 1000,
) -> Dict[str, Any]:
    """
    White's Reality Check (2000).
    
    Tests the null hypothesis that the best strategy from a set is 
    no better than the benchmark, after accounting for data snooping.
    
    Parameters
    ----------
    strategy_returns : np.ndarray, shape (n_strategies, n_periods)
        Matrix of strategy returns. Each row is a strategy, each column a time period.
    benchmark_returns : np.ndarray, shape (n_periods,), optional
        Benchmark returns. If None, zero returns are used (testing if strategies
        have positive expected returns).
    n_bootstrap : int
        Number of bootstrap resamples (default 1000).
    
    Returns
    -------
    dict with test statistic, p-value, and interpretation.
    """
    if strategy_returns.ndim == 1:
        strategy_returns = strategy_returns.reshape(1, -1)
    
    n_strategies, n_periods = strategy_returns.shape
    
    if benchmark_returns is None:
        benchmark_returns = np.zeros(n_periods)
    
    # Excess returns over benchmark
    excess_returns = strategy_returns - benchmark_returns[np.newaxis, :]
    
    # Observed test statistic: max of mean excess returns
    observed_means = np.mean(excess_returns, axis=1)
    observed_stat = np.max(observed_means)
    
    # Bootstrap distribution under the null
    # Center each strategy's excess returns to have zero mean (impose the null)
    centered = excess_returns - observed_means[:, np.newaxis]
    bootstrap_stats = _compute_bootstrap_statistic(centered, n_bootstrap, "white")
    
    # P-value: proportion of bootstrap stats >= observed
    p_value = float(np.mean(bootstrap_stats >= observed_stat))
    
    # Best strategy info
    best_idx = int(np.argmax(observed_means))
    
    return {
        "test": "whites_reality_check",
        "n_strategies": n_strategies,
        "n_periods": n_periods,
        "n_bootstrap": n_bootstrap,
        "observed_statistic": round(float(observed_stat), 6),
        "p_value": round(p_value, 6),
        "is_significant": p_value < 0.05,
        "best_strategy_index": best_idx,
        "best_strategy_mean_excess": round(float(observed_means[best_idx]), 6),
        "interpretation": (
            f"White's Reality Check p-value = {p_value:.4f}. "
            + ("Best strategy is significant after data snooping adjustment." 
               if p_value < 0.05 
               else "Best strategy is NOT significant after data snooping adjustment.")
        ),
    }


def hansen_spa(
    strategy_returns: np.ndarray,
    benchmark_returns: Optional[np.ndarray] = None,
    n_bootstrap: int = 1000,
) -> Dict[str, Any]:
    """
    Hansen's Superior Predictive Ability (SPA) test (2005).
    
    An improvement over White's Reality Check that:
      1. Studentizes the statistics (controls for different variances)
      2. Uses a smarter null centering (only centers strategies with negative means)
    
    Parameters
    ----------
    Same as whites_reality_check.
    
    Returns
    -------
    dict with test statistic, p-value, and interpretation.
    """
    if strategy_returns.ndim == 1:
        strategy_returns = strategy_returns.reshape(1, -1)
    
    n_strategies, n_periods = strategy_returns.shape
    
    if benchmark_returns is None:
        benchmark_returns = np.zeros(n_periods)
    
    excess_returns = strategy_returns - benchmark_returns[np.newaxis, :]
    
    # Observed test statistic (studentized)
    observed_means = np.mean(excess_returns, axis=1)
    observed_stds = np.std(excess_returns, axis=1, ddof=1)
    observed_stds = np.where(observed_stds > 0, observed_stds, 1e-10)
    observed_stat = np.max(observed_means / (observed_stds / np.sqrt(n_periods)))
    
    # Hansen's centering: only center strategies with negative mean (to be conservative)
    # Strategies with positive mean are not forced to have zero mean
    centered_means = np.where(observed_means < 0, observed_means, 0.0)
    centered = excess_returns - centered_means[:, np.newaxis]
    
    bootstrap_stats = _compute_bootstrap_statistic(centered, n_bootstrap, "hansen")
    
    p_value = float(np.mean(bootstrap_stats >= observed_stat))
    best_idx = int(np.argmax(observed_means))
    
    return {
        "test": "hansen_spa",
        "n_strategies": n_strategies,
        "n_periods": n_periods,
        "n_bootstrap": n_bootstrap,
        "observed_statistic": round(float(observed_stat), 6),
        "p_value": round(p_value, 6),
        "is_significant": p_value < 0.05,
        "best_strategy_index": best_idx,
        "best_strategy_mean_excess": round(float(observed_means[best_idx]), 6),
        "interpretation": (
            f"Hansen's SPA p-value = {p_value:.4f}. "
            + ("Best strategy has superior predictive ability after data snooping adjustment." 
               if p_value < 0.05 
               else "Best strategy does NOT have superior predictive ability after adjustment.")
        ),
    }


def run_significance_tests(
    strategy_returns: np.ndarray,
    benchmark_returns: Optional[np.ndarray] = None,
    n_bootstrap: int = 1000,
) -> Dict[str, Any]:
    """
    Run both White's RC and Hansen's SPA and return combined results.
    """
    wrc = whites_reality_check(strategy_returns, benchmark_returns, n_bootstrap)
    spa = hansen_spa(strategy_returns, benchmark_returns, n_bootstrap)
    
    return {
        "whites_reality_check": wrc,
        "hansen_spa": spa,
        "consensus": {
            "both_significant": wrc["is_significant"] and spa["is_significant"],
            "either_significant": wrc["is_significant"] or spa["is_significant"],
            "recommendation": (
                "Both tests confirm significance — strong evidence of genuine alpha."
                if wrc["is_significant"] and spa["is_significant"]
                else "Only one test finds significance — marginal evidence, exercise caution."
                if wrc["is_significant"] or spa["is_significant"]
                else "Neither test finds significance — results likely due to data snooping."
            ),
        },
    }
