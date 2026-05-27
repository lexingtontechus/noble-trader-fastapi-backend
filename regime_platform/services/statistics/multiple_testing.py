"""
Multiple Testing Correction for backtest optimization results.

When testing N strategy variations, the probability of finding at least one
"significant" result by chance alone increases with N. These corrections
adjust p-values to control for this multiple testing problem.

Methods:
  1. Bonferroni - most conservative, divides alpha by N
  2. Holm-Bonferroni - step-down, slightly less conservative
  3. Benjamini-Hochberg FDR - controls false discovery rate

Reference:
  - Bonferroni (1936)
  - Holm (1979) "A simple sequentially rejective multiple test procedure"
  - Benjamini & Hochberg (1995) "Controlling the false discovery rate"
"""

import numpy as np
from typing import Dict, Any, List, Tuple


def bonferroni_correction(p_values: List[float], alpha: float = 0.05) -> Dict[str, Any]:
    """
    Apply Bonferroni correction - the most conservative method.
    
    Adjusted p-value = min(p * N, 1.0)
    """
    n = len(p_values)
    adjusted = [min(p * n, 1.0) for p in p_values]
    significant = [p < alpha for p in adjusted]
    
    return {
        "method": "bonferroni",
        "alpha": alpha,
        "n_tests": n,
        "adjusted_p_values": [round(p, 6) for p in adjusted],
        "is_significant": significant,
        "n_significant": sum(significant),
    }


def holm_bonferroni_correction(p_values: List[float], alpha: float = 0.05) -> Dict[str, Any]:
    """
    Apply Holm-Bonferroni (step-down) correction.
    
    Less conservative than Bonferroni but still controls family-wise error rate.
    Sorts p-values ascending, then adjusts each by (N - rank + 1).
    """
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    
    adjusted = [0.0] * n
    prev_adj = 0.0
    for rank, (orig_idx, p) in enumerate(indexed):
        adj = max(p * (n - rank), prev_adj)  # Enforce monotonicity
        adj = min(adj, 1.0)
        adjusted[orig_idx] = adj
        prev_adj = adj
    
    significant = [p < alpha for p in adjusted]
    
    return {
        "method": "holm_bonferroni",
        "alpha": alpha,
        "n_tests": n,
        "adjusted_p_values": [round(p, 6) for p in adjusted],
        "is_significant": significant,
        "n_significant": sum(significant),
    }


def benjamini_hochberg_fdr(p_values: List[float], alpha: float = 0.05) -> Dict[str, Any]:
    """
    Apply Benjamini-Hochberg FDR correction.
    
    Controls the False Discovery Rate (FDR) - the expected proportion of
    false positives among rejected hypotheses. Less conservative than
    family-wise error rate methods (Bonferroni, Holm).
    
    Procedure:
      1. Sort p-values ascending
      2. Find largest k where p(k) <= k/N * alpha
      3. Reject hypotheses 1..k
    """
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    
    adjusted = [0.0] * n
    # Step-up procedure
    prev_adj = 1.0
    for rank in range(n - 1, -1, -1):
        orig_idx, p = indexed[rank]
        adj = min(p * n / (rank + 1), prev_adj)  # Enforce monotonicity from right
        adj = min(adj, 1.0)
        adjusted[orig_idx] = adj
        prev_adj = adj
    
    significant = [p < alpha for p in adjusted]
    
    # Find BH threshold
    sorted_pvals = sorted(p_values)
    threshold = 0.0
    for k in range(1, n + 1):
        if sorted_pvals[k - 1] <= k / n * alpha:
            threshold = sorted_pvals[k - 1]
    
    return {
        "method": "benjamini_hochberg",
        "alpha": alpha,
        "n_tests": n,
        "adjusted_p_values": [round(p, 6) for p in adjusted],
        "is_significant": significant,
        "n_significant": sum(significant),
        "fdr_threshold": round(threshold, 6),
    }


def apply_all_corrections(p_values: List[float], alpha: float = 0.05) -> Dict[str, Any]:
    """
    Apply all three correction methods and return a combined result.
    
    Parameters
    ----------
    p_values : list[float]
        Raw p-values from N independent hypothesis tests.
    alpha : float
        Significance level (default 0.05).
    
    Returns
    -------
    dict with keys for each method plus a summary.
    """
    bonf = bonferroni_correction(p_values, alpha)
    holm = holm_bonferroni_correction(p_values, alpha)
    bh = benjamini_hochberg_fdr(p_values, alpha)
    
    # Summary: how many are significant under each method
    raw_significant = sum(1 for p in p_values if p < alpha)
    
    return {
        "raw_p_values": [round(p, 6) for p in p_values],
        "n_raw_significant": raw_significant,
        "corrections": {
            "bonferroni": bonf,
            "holm_bonferroni": holm,
            "benjamini_hochberg": bh,
        },
        "summary": {
            "raw_significant": raw_significant,
            "bonferroni_significant": bonf["n_significant"],
            "holm_significant": holm["n_significant"],
            "bh_fdr_significant": bh["n_significant"],
            "interpretation": (
                f"Of {raw_significant} strategies significant at p<{alpha} (uncorrected), "
                f"Bonferroni retains {bonf['n_significant']}, "
                f"Holm retains {holm['n_significant']}, "
                f"BH FDR retains {bh['n_significant']} after correction."
            ),
        },
    }
