"""
Bootstrap Confidence Intervals (Phase 6B).

Provides percentile bootstrap and circular block bootstrap methods for
computing confidence intervals on backtest performance metrics. This is
the statistical foundation that DSR, WRC, and SPA all build upon.

Key features:
  - Percentile bootstrap: simple resampling with replacement
  - Circular block bootstrap: preserves serial correlation in returns
  - Computes CIs for Sharpe, max drawdown, win rate, profit factor,
    and any arbitrary metric via a callable
  - Returns BootstrapResult with point estimate, CI bounds, and resample count

References:
  - Efron & Tibshirani (1993), "An Introduction to the Bootstrap"
  - Politis & Romano (1994), "The Stationary Bootstrap", JASA
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np


# ── Result type ──────────────────────────────────────────────────────


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
    std_error: float = 0.0
    bias: float = 0.0
    sample_size: int = 0

    @property
    def ci_width(self) -> float:
        """Width of the confidence interval."""
        return self.ci_upper - self.ci_lower

    @property
    def ci_relative_width(self) -> float:
        """CI width as a fraction of the point estimate."""
        if abs(self.point_estimate) < 1e-10:
            return float("inf")
        return self.ci_width / abs(self.point_estimate)

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "point_estimate": round(self.point_estimate, 6),
            "ci_lower": round(self.ci_lower, 6),
            "ci_upper": round(self.ci_upper, 6),
            "confidence_level": self.confidence_level,
            "n_resamples": self.n_resamples,
            "method": self.method,
            "std_error": round(self.std_error, 6),
            "bias": round(self.bias, 6),
            "sample_size": self.sample_size,
            "ci_width": round(self.ci_width, 6),
            "ci_relative_width": round(self.ci_relative_width, 6),
        }

    def format_display(self) -> str:
        """Format as '1.45 [1.12, 1.78]' for display."""
        return f"{self.point_estimate:.2f} [{self.ci_lower:.2f}, {self.ci_upper:.2f}]"


# ── Metric computation functions ────────────────────────────────────


def _sharpe_ratio(returns: np.ndarray) -> float:
    """Compute Sharpe ratio (non-annualized) from a returns array."""
    if len(returns) < 2:
        return 0.0
    std = np.std(returns, ddof=1)
    if std < 1e-10:
        return 0.0
    return float(np.mean(returns) / std)


def _win_rate(pnls: np.ndarray) -> float:
    """Compute win rate from P&L array."""
    if len(pnls) == 0:
        return 0.0
    return float(np.sum(pnls > 0) / len(pnls))


def _profit_factor(pnls: np.ndarray) -> float:
    """Compute profit factor from P&L array."""
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 0.001
    if gross_loss < 1e-10:
        return 999.0
    return gross_profit / gross_loss


def _max_drawdown(pnls: np.ndarray) -> float:
    """Compute max drawdown (as a positive fraction) from P&L array."""
    if len(pnls) == 0:
        return 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    return float(np.max(dd))


def _mean_return(returns: np.ndarray) -> float:
    """Compute mean return."""
    if len(returns) == 0:
        return 0.0
    return float(np.mean(returns))


def compute_metric_on_sample(
    sample: np.ndarray,
    metric_name: str,
) -> float:
    """
    Compute a named metric on a sample array.

    Supported metrics:
      - 'sharpe', 'sharpe_ratio'
      - 'win_rate'
      - 'profit_factor'
      - 'max_drawdown'
      - 'mean_return'
      - 'std'
      - 'mean'

    For Sharpe, win_rate, profit_factor, max_drawdown, mean_return,
    the input should be a P&L or returns array.
    """
    metric_name_lower = metric_name.lower().replace(" ", "_")

    if metric_name_lower in ("sharpe", "sharpe_ratio"):
        return _sharpe_ratio(sample)
    elif metric_name_lower == "win_rate":
        return _win_rate(sample)
    elif metric_name_lower == "profit_factor":
        return _profit_factor(sample)
    elif metric_name_lower == "max_drawdown":
        return _max_drawdown(sample)
    elif metric_name_lower == "mean_return":
        return _mean_return(sample)
    elif metric_name_lower == "std":
        return float(np.std(sample, ddof=1)) if len(sample) > 1 else 0.0
    elif metric_name_lower == "mean":
        return float(np.mean(sample)) if len(sample) > 0 else 0.0
    else:
        raise ValueError(
            f"Unknown metric '{metric_name}'. "
            f"Supported: sharpe, win_rate, profit_factor, max_drawdown, mean_return, std, mean"
        )


# ── Percentile Bootstrap ────────────────────────────────────────────


def bootstrap_ci(
    data: Sequence[float] | np.ndarray,
    metric_fn: Callable[[np.ndarray], float] | str = "sharpe",
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    seed: int | None = None,
) -> BootstrapResult:
    """
    Compute bootstrap confidence interval using the percentile method.

    Resamples the data with replacement and computes the metric on each
    resample. The confidence interval is the [alpha/2, 1-alpha/2] quantile
    of the bootstrap distribution.

    Parameters
    ----------
    data : array-like
        The input data (typically trade P&Ls or returns).
    metric_fn : callable or str
        Either a function that takes a numpy array and returns a scalar,
        or a string naming a built-in metric ('sharpe', 'win_rate', etc.).
    n_resamples : int
        Number of bootstrap resamples (default 2000).
    confidence_level : float
        Confidence level for the interval (default 0.95).
    seed : int, optional
        Random seed for reproducibility.

    Returns
    -------
    BootstrapResult with point estimate, CI bounds, and metadata.

    Example
    -------
    >>> pnls = [1.2, -0.5, 2.1, -0.3, 0.8, 1.5, -0.1, 0.9]
    >>> result = bootstrap_ci(pnls, metric_fn="sharpe", n_resamples=1000)
    >>> print(result.format_display())
    """
    data = np.asarray(data, dtype=float)
    n = len(data)

    if n < 2:
        return BootstrapResult(
            metric_name=metric_fn if isinstance(metric_fn, str) else "custom",
            point_estimate=0.0,
            ci_lower=0.0,
            ci_upper=0.0,
            confidence_level=confidence_level,
            n_resamples=0,
            method="percentile",
            sample_size=n,
        )

    # Resolve metric function
    if isinstance(metric_fn, str):
        metric_name = metric_fn
        metric_callable = lambda x: compute_metric_on_sample(x, metric_name)
    else:
        metric_name = "custom"
        metric_callable = metric_fn

    # Point estimate on original data
    point_estimate = metric_callable(data)

    # Bootstrap resampling
    rng = np.random.default_rng(seed)
    bootstrap_estimates = np.empty(n_resamples)

    for i in range(n_resamples):
        sample = rng.choice(data, size=n, replace=True)
        bootstrap_estimates[i] = metric_callable(sample)

    # Percentile CI
    alpha = 1.0 - confidence_level
    ci_lower = float(np.percentile(bootstrap_estimates, 100 * alpha / 2))
    ci_upper = float(np.percentile(bootstrap_estimates, 100 * (1 - alpha / 2)))

    # Bias = mean(bootstrap) - point_estimate
    bootstrap_mean = float(np.mean(bootstrap_estimates))
    bias = bootstrap_mean - point_estimate

    # Standard error of the bootstrap distribution
    std_error = float(np.std(bootstrap_estimates, ddof=1))

    return BootstrapResult(
        metric_name=metric_name,
        point_estimate=point_estimate,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        method="percentile",
        std_error=std_error,
        bias=bias,
        sample_size=n,
    )


# ── Circular Block Bootstrap ────────────────────────────────────────


def circular_block_bootstrap_ci(
    data: Sequence[float] | np.ndarray,
    metric_fn: Callable[[np.ndarray], float] | str = "sharpe",
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    block_size: int | None = None,
    seed: int | None = None,
) -> BootstrapResult:
    """
    Compute bootstrap CI using the circular block bootstrap method.

    The circular block bootstrap wraps the data around to form a circle,
    then samples blocks of contiguous observations. This preserves the
    serial correlation structure that the standard percentile bootstrap
    ignores — critical for time-series returns where order matters.

    Block size is automatically determined using the Politis & Romano
    rule-of-thumb: b = n^(1/3) if not specified.

    Parameters
    ----------
    data : array-like
        The input data (typically returns or trade P&Ls in temporal order).
    metric_fn : callable or str
        Metric function or name (same as bootstrap_ci).
    n_resamples : int
        Number of bootstrap resamples (default 2000).
    confidence_level : float
        Confidence level for the interval (default 0.95).
    block_size : int, optional
        Block size for resampling. Default: n^(1/3) rounded up.
    seed : int, optional
        Random seed for reproducibility.

    Returns
    -------
    BootstrapResult with point estimate, CI bounds, and metadata.

    References
    ----------
    Politis, D.N. & Romano, J.P. (1994). "The Stationary Bootstrap",
    Journal of the American Statistical Association, 89(428), 1303-1313.
    """
    data = np.asarray(data, dtype=float)
    n = len(data)

    if n < 2:
        return BootstrapResult(
            metric_name=metric_fn if isinstance(metric_fn, str) else "custom",
            point_estimate=0.0,
            ci_lower=0.0,
            ci_upper=0.0,
            confidence_level=confidence_level,
            n_resamples=0,
            method="circular_block",
            sample_size=n,
        )

    # Resolve metric function
    if isinstance(metric_fn, str):
        metric_name = metric_fn
        metric_callable = lambda x: compute_metric_on_sample(x, metric_name)
    else:
        metric_name = "custom"
        metric_callable = metric_fn

    # Determine block size (Politis & Romano rule-of-thumb: n^(1/3))
    if block_size is None:
        block_size = max(2, int(np.ceil(n ** (1.0 / 3.0))))

    # Point estimate on original data
    point_estimate = metric_callable(data)

    # Circular block bootstrap resampling
    rng = np.random.default_rng(seed)
    bootstrap_estimates = np.empty(n_resamples)

    # Create circular data by wrapping
    circular_data = np.concatenate([data, data[:block_size]])

    for i in range(n_resamples):
        sample_blocks = []
        current_length = 0

        while current_length < n:
            # Random starting position
            start = rng.integers(0, n)
            # Sample a block from the circular data
            end = start + block_size
            block = circular_data[start:end]
            sample_blocks.append(block)
            current_length += len(block)

        # Concatenate and trim to exact length n
        sample = np.concatenate(sample_blocks)[:n]
        bootstrap_estimates[i] = metric_callable(sample)

    # Percentile CI
    alpha = 1.0 - confidence_level
    ci_lower = float(np.percentile(bootstrap_estimates, 100 * alpha / 2))
    ci_upper = float(np.percentile(bootstrap_estimates, 100 * (1 - alpha / 2)))

    bootstrap_mean = float(np.mean(bootstrap_estimates))
    bias = bootstrap_mean - point_estimate
    std_error = float(np.std(bootstrap_estimates, ddof=1))

    return BootstrapResult(
        metric_name=metric_name,
        point_estimate=point_estimate,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        method="circular_block",
        std_error=std_error,
        bias=bias,
        sample_size=n,
    )


# ── Batch CI computation ────────────────────────────────────────────


def compute_all_bootstrap_cis(
    pnls: Sequence[float] | np.ndarray,
    n_resamples: int = 2000,
    confidence_level: float = 0.95,
    seed: int | None = None,
    use_block_bootstrap: bool = True,
) -> Dict[str, BootstrapResult]:
    """
    Compute bootstrap CIs for all standard metrics in one call.

    This is the primary integration point — call this from
    TradeJournal.get_stats() and Backtester._compute_metrics().

    Parameters
    ----------
    pnls : array-like
        Trade P&L values (in dollars or bricks, must be temporally ordered).
    n_resamples : int
        Number of bootstrap resamples per metric.
    confidence_level : float
        Confidence level (default 0.95).
    seed : int, optional
        Random seed for reproducibility.
    use_block_bootstrap : bool
        If True, use circular block bootstrap (preserves serial correlation).
        If False, use standard percentile bootstrap.

    Returns
    -------
    Dict mapping metric_name → BootstrapResult for:
      - sharpe, win_rate, profit_factor, max_drawdown, mean_return
    """
    pnls = np.asarray(pnls, dtype=float)
    metrics = ["sharpe", "win_rate", "profit_factor", "max_drawdown", "mean_return"]
    results = {}

    bootstrap_fn = circular_block_bootstrap_ci if use_block_bootstrap else bootstrap_ci

    for idx, metric in enumerate(metrics):
        # Use a different seed offset for each metric to avoid correlation
        metric_seed = (seed + idx) if seed is not None else None
        results[metric] = bootstrap_fn(
            data=pnls,
            metric_fn=metric,
            n_resamples=n_resamples,
            confidence_level=confidence_level,
            seed=metric_seed,
        )

    return results
