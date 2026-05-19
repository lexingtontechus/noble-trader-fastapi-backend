"""
Deflated Sharpe Ratio (Phase 6A).

Implements Lopez de Prado's Deflated Sharpe Ratio (DSR), which adjusts
the Sharpe ratio for the multiple testing inherent in strategy selection.
When you test N parameter combinations and pick the best Sharpe, the DSR
tells you the probability that the best Sharpe is genuinely positive
rather than a statistical fluke.

Formula:
    DSR = Phi( (SR_hat * sqrt(T-1) - sqrt(ln(N))) / sqrt(var_hat_SR) )

Where:
    - SR_hat = observed Sharpe ratio (annualized)
    - T      = number of observations (returns)
    - N      = number of independent trials (parameter combinations)
    - var_hat_SR = estimated variance of Sharpe ratio
                  = (1 - skew * SR + (kurt-1)/4 * SR^2) / T
    - Phi   = standard normal CDF

DSR is a probability in [0, 1]. A DSR > 0.95 means we can reject the
null hypothesis that the best Sharpe arose purely from multiple testing
at the 5% significance level.

References:
    - Lopez de Prado, M. & Bailey, D. (2014). "The Deflated Sharpe Ratio",
      SSRN Working Paper.
    - Harvey, C. et al. (2016). "...and the Cross-Section of Expected Returns",
      Review of Financial Studies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from scipy.stats import norm


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class DeflatedSharpeResult:
    """Result of a Deflated Sharpe Ratio computation."""

    dsr: float                    # Deflated Sharpe Ratio (probability)
    raw_sharpe: float             # Observed Sharpe ratio (annualized)
    n_trials: int                 # Number of independent trials
    n_observations: int           # Number of return observations (T)
    skewness: float               # Skewness of returns
    kurtosis: float               # Excess kurtosis of returns
    var_sharpe: float             # Estimated variance of Sharpe ratio
    sharpe_threshold: float       # Minimum Sharpe to be significant at 5%
    is_significant: bool          # DSR > 0.95 (5% significance)
    interpretation: str           # Human-readable interpretation

    def to_dict(self) -> dict:
        return {
            "dsr": round(self.dsr, 6),
            "raw_sharpe": round(self.raw_sharpe, 6),
            "n_trials": self.n_trials,
            "n_observations": self.n_observations,
            "skewness": round(self.skewness, 6),
            "kurtosis": round(self.kurtosis, 6),
            "var_sharpe": round(self.var_sharpe, 8),
            "sharpe_threshold": round(self.sharpe_threshold, 6),
            "is_significant": self.is_significant,
            "interpretation": self.interpretation,
        }


# ── Core DSR computation ────────────────────────────────────────────


def deflated_sharpe_ratio(
    returns: Sequence[float] | np.ndarray,
    n_trials: int = 1,
    annualize: bool = True,
    periods_per_year: int = 252,
) -> DeflatedSharpeResult:
    """
    Compute the Deflated Sharpe Ratio for a strategy's return series.

    Parameters
    ----------
    returns : array-like
        Strategy returns (daily, trade-level, or any frequency).
        Must be the *best* strategy's returns after selection from n_trials.
    n_trials : int
        Number of independent parameter combinations tested (default 1).
        This is the key adjustment factor — more trials → lower DSR.
    annualize : bool
        Whether to annualize the Sharpe ratio (default True).
        If True, assumes returns are at the frequency specified by
        periods_per_year.
    periods_per_year : int
        Number of return periods per year for annualization (default 252).

    Returns
    -------
    DeflatedSharpeResult with DSR probability, raw Sharpe, and metadata.

    Example
    -------
    >>> returns = np.random.normal(0.001, 0.02, 1000)  # Simulated daily returns
    >>> result = deflated_sharpe_ratio(returns, n_trials=50)
    >>> print(f"DSR = {result.dsr:.2%}, Significant = {result.is_significant}")
    """
    returns = np.asarray(returns, dtype=float)
    T = len(returns)

    if T < 2:
        return _trivial_dsr(n_trials)

    # Compute moments of the return distribution
    mean_r = float(np.mean(returns))
    std_r = float(np.std(returns, ddof=1))

    if std_r < 1e-10:
        return _trivial_dsr(n_trials)

    # Raw Sharpe ratio
    raw_sharpe = mean_r / std_r

    # Annualize if requested
    if annualize:
        raw_sharpe_annualized = raw_sharpe * math.sqrt(periods_per_year)
    else:
        raw_sharpe_annualized = raw_sharpe

    # Skewness and excess kurtosis
    skewness = float(_skewness(returns))
    kurtosis = float(_excess_kurtosis(returns))

    # Estimated variance of the Sharpe ratio
    # var_hat_SR = (1 - skew * SR + (kurt-1)/4 * SR^2) / T
    # Using the annualized Sharpe for the threshold calculation
    SR = raw_sharpe  # Use non-annualized for variance estimation
    var_sharpe = (1.0 - skewness * SR + (kurtosis - 1.0) / 4.0 * SR ** 2) / T

    if var_sharpe < 1e-15:
        var_sharpe = 1.0 / T  # Fallback for near-zero variance

    # Sharpe threshold: the minimum Sharpe that would be statistically
    # significant given N trials at the 5% level
    # E[max(SR)] ≈ sqrt(ln(N)) / sqrt(T) * (1 - gamma) where gamma ≈ 0.5772
    # Simplified threshold: sqrt(ln(N)) / sqrt(T) as per Lopez de Prado
    n = max(n_trials, 1)
    if n == 1:
        sharpe_threshold = 0.0  # No multiple testing penalty for single trial
    else:
        sharpe_threshold = math.sqrt(math.log(n)) / math.sqrt(T)
        if annualize:
            sharpe_threshold_annualized = sharpe_threshold * math.sqrt(periods_per_year)
        else:
            sharpe_threshold_annualized = sharpe_threshold

    # DSR computation
    # DSR = Phi( (SR_hat * sqrt(T-1) - sqrt(ln(N))) / sqrt(var_hat_SR) )
    if n == 1:
        # Single trial: just test if Sharpe > 0
        # Standard t-test: SR * sqrt(T) / sqrt(var_hat_SR)
        z = raw_sharpe * math.sqrt(T) / math.sqrt(var_sharpe) if var_sharpe > 0 else 0.0
    else:
        z_num = raw_sharpe * math.sqrt(T - 1) - math.sqrt(math.log(n))
        z = z_num / math.sqrt(var_sharpe) if var_sharpe > 0 else 0.0

    dsr = float(norm.cdf(z))

    # Significance test
    is_significant = dsr > 0.95

    # Interpretation
    if is_significant:
        interpretation = (
            f"DSR = {dsr:.1%}: The observed Sharpe ({raw_sharpe_annualized:.2f}) "
            f"is statistically significant after adjusting for {n_trials} trials. "
            f"There is a {dsr:.1%} probability this strategy genuinely has positive "
            f"risk-adjusted returns rather than being a multiple-testing artifact."
        )
    elif dsr > 0.5:
        interpretation = (
            f"DSR = {dsr:.1%}: The observed Sharpe ({raw_sharpe_annualized:.2f}) "
            f"is suggestive but not statistically significant at the 5% level after "
            f"adjusting for {n_trials} trials. Consider reducing the parameter space "
            f"or gathering more out-of-sample evidence."
        )
    else:
        interpretation = (
            f"DSR = {dsr:.1%}: The observed Sharpe ({raw_sharpe_annualized:.2f}) "
            f"is likely a multiple-testing artifact from {n_trials} trials. "
            f"The strategy does not pass statistical scrutiny — proceed with caution."
        )

    # Use annualized threshold for display if annualize is True
    if n == 1:
        threshold_display = 0.0
    elif annualize:
        threshold_display = sharpe_threshold_annualized
    else:
        threshold_display = sharpe_threshold

    return DeflatedSharpeResult(
        dsr=dsr,
        raw_sharpe=round(raw_sharpe_annualized, 6),
        n_trials=n_trials,
        n_observations=T,
        skewness=round(skewness, 6),
        kurtosis=round(kurtosis, 6),
        var_sharpe=round(var_sharpe, 8),
        sharpe_threshold=round(threshold_display, 6),
        is_significant=is_significant,
        interpretation=interpretation,
    )


# ── Convenience: DSR for a full backtest ────────────────────────────


def compute_dsr_for_backtest(
    trade_pnls: Sequence[float] | np.ndarray,
    n_trials: int = 1,
    is_daily_returns: bool = False,
) -> DeflatedSharpeResult:
    """
    Compute DSR for a backtest result given its trade P&L series.

    This is a convenience wrapper that handles the trade-level vs
    daily-level return distinction.

    Parameters
    ----------
    trade_pnls : array-like
        Trade P&L values. If is_daily_returns is False (default), these
        are per-trade P&Ls. If True, these are daily returns.
    n_trials : int
        Number of parameter combinations tested (from optimization).
    is_daily_returns : bool
        If True, treat input as daily returns and annualize with sqrt(252).
        If False, treat as trade-level returns and annualize with
        sqrt(n_trades_per_year), estimated from the data.

    Returns
    -------
    DeflatedSharpeResult
    """
    pnls = np.asarray(trade_pnls, dtype=float)

    if is_daily_returns:
        return deflated_sharpe_ratio(
            returns=pnls,
            n_trials=n_trials,
            annualize=True,
            periods_per_year=252,
        )
    else:
        # For trade-level returns, we don't annualize (trades are irregular)
        # but we still adjust for multiple testing
        return deflated_sharpe_ratio(
            returns=pnls,
            n_trials=n_trials,
            annualize=False,
        )


# ── Internal helpers ────────────────────────────────────────────────


def _skewness(returns: np.ndarray) -> float:
    """Compute sample skewness (Fisher's definition, bias-corrected)."""
    n = len(returns)
    if n < 3:
        return 0.0
    m = np.mean(returns)
    s = np.std(returns, ddof=1)
    if s < 1e-10:
        return 0.0
    # Bias-corrected skewness
    return float(np.sum(((returns - m) / s) ** 3) * n / ((n - 1) * (n - 2)))


def _excess_kurtosis(returns: np.ndarray) -> float:
    """Compute sample excess kurtosis (bias-corrected)."""
    n = len(returns)
    if n < 4:
        return 0.0
    m = np.mean(returns)
    s = np.std(returns, ddof=1)
    if s < 1e-10:
        return 0.0
    m4 = np.sum(((returns - m) / s) ** 4)
    # Bias-corrected excess kurtosis
    kurt = ((n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))) * m4 - \
           (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))
    return float(kurt)


def _trivial_dsr(n_trials: int) -> DeflatedSharpeResult:
    """Return a trivial DSR result for edge cases (too few returns)."""
    return DeflatedSharpeResult(
        dsr=0.0,
        raw_sharpe=0.0,
        n_trials=n_trials,
        n_observations=0,
        skewness=0.0,
        kurtosis=0.0,
        var_sharpe=0.0,
        sharpe_threshold=0.0,
        is_significant=False,
        interpretation="Insufficient data to compute DSR (need at least 2 returns).",
    )
