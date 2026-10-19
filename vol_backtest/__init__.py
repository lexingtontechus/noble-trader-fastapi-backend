"""vol_backtest — Phase 0 evaluation framework for realized-volatility
forecasting (TimesFM refined thesis, corrected 2026-10-18).

Layout:
    realized.py    RV computation, per-asset calendars, synthetic data
    metrics.py     QLIKE / log-scale MAE-RMSE / directional vs persistence /
                   interval coverage / pinball
    dm.py          Diebold-Mariano with Newey-West HAC + Holm correction
    arms.py        baselines: naive, EWMA, Log-HAR (the bar), har_ewma blend,
                   optional GARCH(1,1)
    model_arms.py  optional TSFM arms: TimesFM 2.5 (repo server :8002),
                   Chronos-2 univariate, Chronos-2 covariate (endgame arm)
    data.py        CSV / yfinance / synthetic loaders
    harness.py     rolling-origin backtest with frozen cutoffs

Quick start:
    python scripts/run_vol_backtest.py --synthetic --horizons 24 --cutoffs 40

Nothing in this package touches the live orchestrator, EV engine, or gates —
it is research tooling kept OUTSIDE regime_platform on purpose (dead-weight
audit 2026-10-13: research code does not belong in the prod package).
"""
from .arms import Arm, ArmForecast, NaiveRV, EWMARV, LogHAR, HAR_EWMA, GARCH11
from .realized import SymbolSeries, log_returns, rv_trailing, forward_rv
from .harness import BacktestResult, backtest_symbol, aggregate_metrics, dm_family

__all__ = [
    "Arm", "ArmForecast", "NaiveRV", "EWMARV", "LogHAR", "HAR_EWMA", "GARCH11",
    "SymbolSeries", "log_returns", "rv_trailing", "forward_rv",
    "BacktestResult", "backtest_symbol", "aggregate_metrics", "dm_family",
]
