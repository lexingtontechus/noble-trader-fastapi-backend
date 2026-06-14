"""
Statistical Rigor Module for Noble Trader Backtesting Platform.

Provides tools for validating backtest results with statistical rigor:
  - Deflated Sharpe Ratio (DSR): Adjusts observed Sharpe for multiple testing
  - Bootstrap Confidence Intervals: Percentile and circular block bootstrap CIs
  - Multiple Testing Correction: Bonferroni, Holm-Bonferroni, Benjamini-Hochberg FDR
  - Significance Tests: White's Reality Check and Hansen's SPA
  - EV Engine: Blended P_win from regime + imbalance + Markov transitions
  - Markov Chain: Discrete price state transition tracking
  - Bayesian Updater: Live P_win updates based on in-session trade outcomes
"""

from regime_platform.services.statistics.deflated_sharpe import deflated_sharpe_ratio
from regime_platform.services.statistics.bootstrap_ci import (
    percentile_bootstrap_ci,
    circular_block_bootstrap_ci,
    compute_all_bootstrap_cis,
    BootstrapResult,
    METRIC_FUNCTIONS,
)
from regime_platform.services.statistics.multiple_testing import (
    bonferroni_correction,
    holm_bonferroni_correction,
    benjamini_hochberg_fdr,
    apply_all_corrections,
)
from regime_platform.services.statistics.significance_tests import (
    whites_reality_check,
    hansen_spa,
    run_significance_tests,
)
from regime_platform.services.statistics.ev_engine import (
    EVEngine,
    EVResult,
    EVFullResult,
)
from regime_platform.services.statistics.markov_chain import MarkovChain
from regime_platform.services.statistics.bayesian_updater import BayesianUpdater

__all__ = [
    # Deflated Sharpe
    "deflated_sharpe_ratio",
    # Bootstrap CI
    "percentile_bootstrap_ci",
    "circular_block_bootstrap_ci",
    "compute_all_bootstrap_cis",
    "BootstrapResult",
    "METRIC_FUNCTIONS",
    # Multiple Testing
    "bonferroni_correction",
    "holm_bonferroni_correction",
    "benjamini_hochberg_fdr",
    "apply_all_corrections",
    # Significance Tests
    "whites_reality_check",
    "hansen_spa",
    "run_significance_tests",
    # EV Engine
    "EVEngine",
    "EVResult",
    "EVFullResult",
    # Markov Chain
    "MarkovChain",
    # Bayesian Updater
    "BayesianUpdater",
]
