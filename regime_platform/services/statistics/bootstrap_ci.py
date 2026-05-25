"""
Bootstrap Confidence Intervals for backtest metrics.

Computes confidence intervals for Sharpe ratio, win rate, profit factor,
and max drawdown using:
  1. Percentile bootstrap (resample with replacement)
  2. Circular block bootstrap (preserves dependency structure)

Reference: Politis & Romano (1994) "The Stationary Bootstrap"
"""

import numpy as np
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass


@dataclass
class BootstrapResult:
    """Result of a bootstrap confidence interval computation."""
    metric_name: str
    point_estimate: float
    ci_lower: float
    ci_upper: float
    confidence_level: float
    n_resamples: int
    method: str  # "percentile" or "circular_block"
    se_estimate: float  # standard error from bootstrap distribution


def _sharpe_ratio(returns: np.ndarray) -> float:
    """Compute Sharpe ratio from a return series."""
    if len(returns) < 2 or np.std(returns) == 0:
        return 0.0
    return float(np.mean(returns) / np.std(returns)) * np.sqrt(252)


def _win_rate(pnls: np.ndarray) -> float:
    """Compute win rate from P&L series."""
    if len(pnls) == 0:
        return 0.0
    return float(np.mean(pnls > 0))


def _profit_factor(pnls: np.ndarray) -> float:
    """Compute profit factor from P&L series."""
    gains = pnls[pnls > 0].sum()
    losses = abs(pnls[pnls < 0].sum())
    if losses == 0:
        return float('inf') if gains > 0 else 0.0
    return float(gains / losses)


def _max_drawdown(pnls: np.ndarray) -> float:
    """Compute max drawdown from P&L series."""
    cumulative = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    return float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0


# Metric registry
METRIC_FUNCTIONS = {
    "sharpe_ratio": _sharpe_ratio,
    "win_rate": _win_rate,
    "profit_factor": _profit_factor,
    "max_drawdown": _max_drawdown,
}


def percentile_bootstrap_ci(
    values: np.ndarray,
    metric_fn: Callable[[np.ndarray], float],
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    metric_name: str = "unknown",
) -> BootstrapResult:
    """
    Compute percentile bootstrap confidence interval.
    
    Resamples the input values with replacement N times, computes the 
    metric for each resample, and takes the quantiles as the CI.
    """
    point_estimate = metric_fn(values)
    bootstrap_estimates = np.empty(n_resamples)
    
    n = len(values)
    for i in range(n_resamples):
        sample = np.random.choice(values, size=n, replace=True)
        bootstrap_estimates[i] = metric_fn(sample)
    
    alpha = 1 - confidence_level
    ci_lower = float(np.percentile(bootstrap_estimates, 100 * alpha / 2))
    ci_upper = float(np.percentile(bootstrap_estimates, 100 * (1 - alpha / 2)))
    se_estimate = float(np.std(bootstrap_estimates))
    
    return BootstrapResult(
        metric_name=metric_name,
        point_estimate=round(point_estimate, 6),
        ci_lower=round(ci_lower, 6),
        ci_upper=round(ci_upper, 6),
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        method="percentile",
        se_estimate=round(se_estimate, 6),
    )


def circular_block_bootstrap_ci(
    values: np.ndarray,
    metric_fn: Callable[[np.ndarray], float],
    block_size: int = 5,
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    metric_name: str = "unknown",
) -> BootstrapResult:
    """
    Compute circular block bootstrap confidence interval.
    
    Preserves dependency structure in the data by sampling blocks
    of consecutive observations rather than individual observations.
    The 'circular' aspect wraps the data around to handle edge effects.
    
    Parameters
    ----------
    block_size : int
        Average block size for the stationary bootstrap. Default 5
        is appropriate for daily returns with moderate autocorrelation.
    """
    point_estimate = metric_fn(values)
    n = len(values)
    bootstrap_estimates = np.empty(n_resamples)
    
    for i in range(n_resamples):
        # Circular block bootstrap: sample blocks of random length
        # Block length follows a geometric distribution with mean = block_size
        resampled = []
        idx = 0
        while len(resampled) < n:
            # Random starting position (circular)
            start = np.random.randint(0, n)
            # Random block length (geometric distribution)
            block_len = min(np.random.geometric(1.0 / block_size), n)
            # Extract block with wrap-around
            for j in range(block_len):
                resampled.append(values[(start + j) % n])
                if len(resampled) >= n:
                    break
        
        bootstrap_estimates[i] = metric_fn(np.array(resampled[:n]))
    
    alpha = 1 - confidence_level
    ci_lower = float(np.percentile(bootstrap_estimates, 100 * alpha / 2))
    ci_upper = float(np.percentile(bootstrap_estimates, 100 * (1 - alpha / 2)))
    se_estimate = float(np.std(bootstrap_estimates))
    
    return BootstrapResult(
        metric_name=metric_name,
        point_estimate=round(point_estimate, 6),
        ci_lower=round(ci_lower, 6),
        ci_upper=round(ci_upper, 6),
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        method="circular_block",
        se_estimate=round(se_estimate, 6),
    )


def compute_all_bootstrap_cis(
    trade_pnls: np.ndarray,
    confidence_level: float = 0.95,
    n_resamples: int = 2000,
    block_size: int = 5,
) -> Dict[str, Any]:
    """
    Compute bootstrap CIs for all key metrics.
    
    Returns a dict with both percentile and circular block results
    for each metric.
    """
    if len(trade_pnls) < 5:
        return {"error": "Insufficient trades for bootstrap CI (need at least 5)"}
    
    results = {}
    for metric_name, metric_fn in METRIC_FUNCTIONS.items():
        pct = percentile_bootstrap_ci(
            trade_pnls, metric_fn, n_resamples, confidence_level, metric_name
        )
        blk = circular_block_bootstrap_ci(
            trade_pnls, metric_fn, block_size, n_resamples, confidence_level, metric_name
        )
        results[metric_name] = {
            "point_estimate": pct.point_estimate,
            "percentile_ci": {
                "lower": pct.ci_lower,
                "upper": pct.ci_upper,
                "se": pct.se_estimate,
            },
            "circular_block_ci": {
                "lower": blk.ci_lower,
                "upper": blk.ci_upper,
                "se": blk.se_estimate,
            },
            "confidence_level": confidence_level,
        }
    
    return results
