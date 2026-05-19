"""
Multiple Testing Correction (Phase 6C).

Implements three standard methods for adjusting p-values when testing
multiple parameter combinations during strategy optimization:

  1. Bonferroni correction — simplest, most conservative
  2. Holm-Bonferroni (step-down) — uniformly more powerful than Bonferroni
  3. Benjamini-Hochberg FDR — controls false discovery rate instead of
     family-wise error rate; less conservative, more powerful

Each method takes a list of (parameter_set, p_value) pairs and returns
adjusted p-values with significance flags.

The `multiple_testing_correction` function runs all three methods and
returns a consolidated result, making it easy to compare approaches.

References:
  - Bonferroni, C.E. (1936), "Teoria statistica delle classi e calcolo
    delle probabilita"
  - Holm, S. (1979), "A simple sequentially rejective multiple test
    procedure", Scandinavian Journal of Statistics
  - Benjamini, Y. & Hochberg, Y. (1995), "Controlling the false discovery
    rate: a practical and powerful approach to multiple testing", JRSS-B
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import norm


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class MultipleTestingResult:
    """Result of a multiple testing correction analysis."""

    method: str
    n_tests: int
    n_significant: int
    alpha: float
    adjusted_p_values: List[Dict[str, any]]  # [{params, raw_p, adjusted_p, is_significant}]
    significant_results: List[Dict[str, any]]  # Only the significant ones
    interpretation: str

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "n_tests": self.n_tests,
            "n_significant": self.n_significant,
            "alpha": self.alpha,
            "adjusted_p_values": self.adjusted_p_values,
            "significant_results": self.significant_results,
            "interpretation": self.interpretation,
        }


# ── Helper: p-value from Sharpe ratio ───────────────────────────────


def sharpe_to_p_value(
    sharpe_ratio: float,
    n_observations: int,
) -> float:
    """
    Convert a Sharpe ratio to a one-sided p-value.

    Under H0: Sharpe = 0, the test statistic SR * sqrt(T) is
    approximately N(0, 1) for large T.

    Parameters
    ----------
    sharpe_ratio : float
        Observed Sharpe ratio (non-annualized).
    n_observations : int
        Number of return observations.

    Returns
    -------
    One-sided p-value testing H0: Sharpe <= 0.
    """
    if n_observations < 2:
        return 1.0
    z = sharpe_ratio * np.sqrt(n_observations)
    return float(1.0 - norm.cdf(z))


# ── Bonferroni Correction ───────────────────────────────────────────


def bonferroni_correction(
    p_values: Sequence[float],
    alpha: float = 0.05,
) -> Tuple[List[float], List[bool]]:
    """
    Apply Bonferroni correction to a list of p-values.

    Adjusted p_i = min(p_i * m, 1.0), where m is the number of tests.

    This is the most conservative correction — controls the family-wise
    error rate (FWER) at level alpha.

    Parameters
    ----------
    p_values : list of float
        Raw p-values from individual tests.
    alpha : float
        Significance level (default 0.05).

    Returns
    -------
    (adjusted_p_values, is_significant) where each is a list of same
    length as p_values.
    """
    m = len(p_values)
    if m == 0:
        return [], []

    adjusted = [min(p * m, 1.0) for p in p_values]
    significant = [p <= alpha for p in adjusted]

    return adjusted, significant


# ── Holm-Bonferroni (Step-Down) Correction ──────────────────────────


def holm_bonferroni_correction(
    p_values: Sequence[float],
    alpha: float = 0.05,
) -> Tuple[List[float], List[bool]]:
    """
    Apply Holm-Bonferroni step-down correction to p-values.

    This method is uniformly more powerful than Bonferroni while still
    controlling FWER. It orders p-values from smallest to largest and
    applies progressively less stringent corrections.

    Algorithm:
      1. Sort p-values: p_(1) <= p_(2) <= ... <= p_(m)
      2. For rank j, adjusted p_(j) = min(p_(j) * (m - j + 1), 1.0)
      3. Enforce monotonicity: if adjusted p_(j) < adjusted p_(j-1),
         set adjusted p_(j) = adjusted p_(j-1)
      4. Reject all H_(j) where adjusted p_(j) <= alpha

    Parameters
    ----------
    p_values : list of float
        Raw p-values.
    alpha : float
        Significance level (default 0.05).

    Returns
    -------
    (adjusted_p_values_in_original_order, is_significant_in_original_order)
    """
    m = len(p_values)
    if m == 0:
        return [], []

    # Sort indices by p-value
    sorted_indices = np.argsort(p_values)
    sorted_p = [p_values[i] for i in sorted_indices]

    # Step-down adjustment
    adjusted_sorted = [0.0] * m
    for j in range(m):
        adj = min(sorted_p[j] * (m - j), 1.0)
        # Enforce monotonicity
        if j > 0 and adj < adjusted_sorted[j - 1]:
            adj = adjusted_sorted[j - 1]
        adjusted_sorted[j] = adj

    # Map back to original order
    adjusted_original = [0.0] * m
    for rank, orig_idx in enumerate(sorted_indices):
        adjusted_original[orig_idx] = adjusted_sorted[rank]

    significant = [p <= alpha for p in adjusted_original]

    return adjusted_original, significant


# ── Benjamini-Hochberg FDR ──────────────────────────────────────────


def benjamini_hochberg_fdr(
    p_values: Sequence[float],
    alpha: float = 0.05,
) -> Tuple[List[float], List[bool]]:
    """
    Apply Benjamini-Hochberg FDR correction to p-values.

    Controls the False Discovery Rate (expected fraction of false
    positives among all discoveries) at level alpha. Less conservative
    than Bonferroni/Holm — more discoveries, but some may be false.

    Algorithm:
      1. Sort p-values: p_(1) <= p_(2) <= ... <= p_(m)
      2. Find largest k such that p_(k) <= k * alpha / m
      3. Reject H_(1), ..., H_(k)
      4. Adjusted p_(j) = min(p_(j) * m / j, 1.0), with monotonicity

    Parameters
    ----------
    p_values : list of float
        Raw p-values.
    alpha : float
        FDR level (default 0.05).

    Returns
    -------
    (adjusted_p_values_in_original_order, is_significant_in_original_order)
    """
    m = len(p_values)
    if m == 0:
        return [], []

    # Sort indices by p-value
    sorted_indices = np.argsort(p_values)
    sorted_p = [p_values[i] for i in sorted_indices]

    # BH adjustment
    adjusted_sorted = [0.0] * m
    for j in range(m - 1, -1, -1):
        rank = j + 1  # 1-based rank
        adj = min(sorted_p[j] * m / rank, 1.0)
        # Enforce monotonicity (step-up)
        if j < m - 1 and adj > adjusted_sorted[j + 1]:
            adj = adjusted_sorted[j + 1]
        adjusted_sorted[j] = adj

    # Map back to original order
    adjusted_original = [0.0] * m
    for rank, orig_idx in enumerate(sorted_indices):
        adjusted_original[orig_idx] = adjusted_sorted[rank]

    significant = [p <= alpha for p in adjusted_original]

    return adjusted_original, significant


# ── Combined: run all three corrections ─────────────────────────────


def multiple_testing_correction(
    results: List[Dict[str, any]],
    alpha: float = 0.05,
    n_observations: int | None = None,
) -> Dict[str, MultipleTestingResult]:
    """
    Run all three multiple testing corrections on optimization results.

    Each result dict should contain:
      - 'params': dict of parameter values
      - 'sharpe_estimate' or 'sharpe_ratio': float (the Sharpe to test)
      - Optionally 'p_value': float (if pre-computed; otherwise computed
        from Sharpe and n_observations)

    Parameters
    ----------
    results : list of dict
        Optimization results, each with params and a Sharpe value.
    alpha : float
        Significance level (default 0.05).
    n_observations : int, optional
        Number of return observations per strategy. Required if p-values
        need to be computed from Sharpe ratios.

    Returns
    -------
    Dict mapping method name → MultipleTestingResult for:
      - 'bonferroni'
      - 'holm_bonferroni'
      - 'benjamini_hochberg'

    Example
    -------
    >>> results = [
    ...     {"params": {"sl": 3}, "sharpe_estimate": 1.2},
    ...     {"params": {"sl": 5}, "sharpe_estimate": 0.8},
    ...     {"params": {"sl": 7}, "sharpe_estimate": 1.5},
    ... ]
    >>> correction = multiple_testing_correction(results, n_observations=500)
    >>> for method, result in correction.items():
    ...     print(f"{method}: {result.n_significant}/{result.n_tests} significant")
    """
    if not results:
        return {}

    # Extract or compute p-values
    p_values = []
    for r in results:
        if "p_value" in r and r["p_value"] is not None:
            p_values.append(float(r["p_value"]))
        else:
            sharpe = float(r.get("sharpe_estimate", r.get("sharpe_ratio", 0.0)))
            n_obs = n_observations or r.get("total_trades", 100)
            p_values.append(sharpe_to_p_value(sharpe, int(n_obs)))

    m = len(results)
    methods = {
        "bonferroni": bonferroni_correction,
        "holm_bonferroni": holm_bonferroni_correction,
        "benjamini_hochberg": benjamini_hochberg_fdr,
    }

    output = {}

    for method_name, correction_fn in methods.items():
        adjusted_p, significant = correction_fn(p_values, alpha)

        # Build per-result records
        adjusted_records = []
        significant_records = []

        for i, r in enumerate(results):
            record = {
                "params": r.get("params", {}),
                "raw_p": round(p_values[i], 6),
                "adjusted_p": round(adjusted_p[i], 6),
                "is_significant": significant[i],
            }
            if "sharpe_estimate" in r:
                record["sharpe_estimate"] = r["sharpe_estimate"]
            elif "sharpe_ratio" in r:
                record["sharpe_ratio"] = r["sharpe_ratio"]
            adjusted_records.append(record)

            if significant[i]:
                significant_records.append(record)

        n_sig = sum(significant)

        # Interpretation
        if n_sig == 0:
            interpretation = (
                f"No strategy passes the {method_name} correction at alpha={alpha}. "
                f"Out of {m} tests, none remain significant after adjusting for "
                f"multiple comparisons. The observed Sharpe ratios may be "
                f"statistical artifacts of overfitting."
            )
        elif n_sig == m:
            interpretation = (
                f"All {m} strategies pass the {method_name} correction at "
                f"alpha={alpha}. This is unusual for optimization results — "
                f"verify that the strategies are genuinely independent."
            )
        else:
            interpretation = (
                f"{n_sig} out of {m} strategies pass the {method_name} "
                f"correction at alpha={alpha}. These strategies show "
                f"statistically significant risk-adjusted returns after "
                f"adjusting for multiple testing."
            )

        output[method_name] = MultipleTestingResult(
            method=method_name,
            n_tests=m,
            n_significant=n_sig,
            alpha=alpha,
            adjusted_p_values=adjusted_records,
            significant_results=significant_records,
            interpretation=interpretation,
        )

    return output
