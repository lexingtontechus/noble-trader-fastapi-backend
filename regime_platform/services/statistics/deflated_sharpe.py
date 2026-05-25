"""
Deflated Sharpe Ratio — adjusts observed Sharpe for multiple testing.

Reference: Lopez de Prado & Bailey (2014) "The Deflated Sharpe Ratio"

The DSR answers: "Is the best Sharpe ratio from N independent trials 
statistically significant, or is it just the expected maximum of N random draws?"

Formula:
    DSR = Phi( (SR_hat * sqrt(T-1) - V(SR)) / sqrt(var(SR_hat)) )
    
where:
    SR_hat = observed Sharpe ratio (annualized)
    T = number of observations (sample length)
    N = number of independent trials
    V(SR) = sqrt( ((1 - skew*SR + (kurt-1)/4 * SR^2) * (N-1)) / T )  -- expected max SR under null
    var(SR_hat) = (1 - skew*SR + (kurt-1)/4 * SR^2) / T  -- variance of SR estimate
    Phi = standard normal CDF
"""

import numpy as np
from scipy import stats as sp_stats
from typing import Optional, Dict, Any


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    sample_length: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    annualize_factor: float = 252,
    returns: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Compute the Deflated Sharpe Ratio.
    
    Parameters
    ----------
    observed_sharpe : float
        The observed (annualized) Sharpe ratio of the best strategy.
    n_trials : int
        Number of independent strategy trials attempted.
    sample_length : int
        Number of return observations used to compute Sharpe.
    skewness : float
        Skewness of the return distribution (0 if unknown).
    kurtosis : float
        Excess kurtosis of the return distribution (3 = normal if unknown).
    annualize_factor : float
        Annualization factor (252 for daily, 52 for weekly).
    returns : np.ndarray, optional
        Raw return series. If provided, skewness/kurtosis are computed from data.
    
    Returns
    -------
    dict with keys:
        dsr: float — Deflated Sharpe Ratio (probability that observed Sharpe is significant)
        observed_sharpe: float — input Sharpe
        n_trials: int — number of trials
        expected_max_sharpe: float — expected maximum Sharpe under null (V(SR))
        p_value: float — 1 - DSR (probability this is due to luck)
        is_significant: bool — True if DSR > 0.95 (5% significance)
        interpretation: str — human-readable interpretation
    """
    # If returns provided, compute moments
    if returns is not None and len(returns) > 2:
        skewness = float(sp_stats.skew(returns))
        kurtosis = float(sp_stats.kurtosis(returns))  # excess kurtosis (normal=0)
        # Convert to raw kurtosis for the formula
        raw_kurtosis = kurtosis + 3.0
    else:
        raw_kurtosis = kurtosis
    
    # De-annualize Sharpe for computation
    sr = observed_sharpe / np.sqrt(annualize_factor)
    
    # Non-normality adjustment
    # var(SR_hat) = (1 - skew*SR + (kurt-1)/4 * SR^2) / T
    sr_variance = (1 - skewness * sr + (raw_kurtosis - 1) / 4 * sr**2) / sample_length
    
    # Expected maximum Sharpe under the null (V(SR))
    # E[max(SR)] = sqrt(V(SR_hat)) * ((1 - gamma) * Phi^{-1}(1 - 1/N) + gamma * Phi^{-1}(1 - 1/(N*e)))
    # Simplified approximation:
    if n_trials > 1:
        # Z_n = sqrt(2 * ln(n)) — expected maximum of N standard normal draws
        z_n = np.sqrt(2 * np.log(n_trials))
        # Correction for finite samples (small N)
        gamma = 0.5772156649  # Euler-Mascheroni constant
        correction = (gamma - np.log(np.log(n_trials))) / (2 * z_n) if n_trials > 2 else 0
        expected_max_sr = z_n * np.sqrt(sr_variance) + correction * np.sqrt(sr_variance)
    else:
        expected_max_sr = 0.0
    
    # DSR computation
    if sr_variance > 0:
        dsr_stat = (sr * np.sqrt(sample_length) - expected_max_sr * np.sqrt(sample_length)) / np.sqrt(sample_length * sr_variance / annualize_factor)
        dsr = float(sp_stats.norm.cdf(dsr_stat))
    else:
        dsr = 0.5  # No variance → no information
    
    p_value = 1 - dsr
    is_significant = dsr > 0.95
    
    # Interpretation
    if dsr > 0.99:
        interpretation = "Highly significant — strong evidence of genuine alpha after multiple testing adjustment"
    elif dsr > 0.95:
        interpretation = "Significant at 5% level — strategy likely has genuine edge after N trials"
    elif dsr > 0.90:
        interpretation = "Borderline — marginal evidence after multiple testing; exercise caution"
    elif dsr > 0.75:
        interpretation = "Not significant — observed Sharpe likely due to selection bias from N trials"
    else:
        interpretation = "Not significant — observed Sharpe is consistent with random chance across N trials"
    
    return {
        "dsr": round(dsr, 6),
        "observed_sharpe": observed_sharpe,
        "n_trials": n_trials,
        "sample_length": sample_length,
        "expected_max_sharpe": round(expected_max_sr * np.sqrt(annualize_factor), 6),
        "p_value": round(p_value, 6),
        "is_significant": is_significant,
        "interpretation": interpretation,
    }
