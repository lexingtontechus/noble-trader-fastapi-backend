"""
Statistical Rigor module (Phase 6).

Provides institutional-grade statistical validation for backtest results:
  6A — Deflated Sharpe Ratio (DSR) — Lopez de Prado's multiple-testing-adjusted Sharpe
  6B — Bootstrap Confidence Intervals — percentile & circular block bootstrap for key metrics
  6C — Multiple Testing Correction — Bonferroni, Holm-Bonferroni, Benjamini-Hochberg FDR
  6D — Strategy Significance Tests — White's Reality Check, Hansen's SPA
"""

from regime_platform.services.statistics.bootstrap_ci import (
    bootstrap_ci,
    circular_block_bootstrap_ci,
    compute_metric_on_sample,
    BootstrapResult,
)
from regime_platform.services.statistics.deflated_sharpe import (
    deflated_sharpe_ratio,
    compute_dsr_for_backtest,
    DeflatedSharpeResult,
)
from regime_platform.services.statistics.multiple_testing import (
    multiple_testing_correction,
    bonferroni_correction,
    holm_bonferroni_correction,
    benjamini_hochberg_fdr,
    MultipleTestingResult,
)
from regime_platform.services.statistics.significance_tests import (
    whites_reality_check,
    hansen_spa,
    SignificanceTestResult,
)

__all__ = [
    # 6B — Bootstrap CIs
    "bootstrap_ci",
    "circular_block_bootstrap_ci",
    "compute_metric_on_sample",
    "BootstrapResult",
    # 6A — Deflated Sharpe Ratio
    "deflated_sharpe_ratio",
    "compute_dsr_for_backtest",
    "DeflatedSharpeResult",
    # 6C — Multiple Testing Correction
    "multiple_testing_correction",
    "bonferroni_correction",
    "holm_bonferroni_correction",
    "benjamini_hochberg_fdr",
    "MultipleTestingResult",
    # 6D — Significance Tests
    "whites_reality_check",
    "hansen_spa",
    "SignificanceTestResult",
]
