"""
FastAPI Router - exposes the Renko HFT pipeline as REST endpoints.

These endpoints can be mounted on the existing FastAPI backend
running on Render, or used standalone.

Mount with::

    from renko.router import renko_router
    app.include_router(renko_router, prefix="/renko", tags=["renko"])

Authentication:
  All endpoints require authentication via the unified auth dependency
  (Clerk JWT -> local JWT -> API key -> dev bypass). Auth levels:
    - get_authed_user: Read-only endpoints - state, stats, bricks, etc. (any role)
    - require_write: Mutations - tick ingestion, backtests, regime, equity (admin/trader)
    - require_admin: Destructive - /config, /reset (admin only)

Discord notifications:
  When a signal is detected or a trade is closed, the router
  automatically sends a rich embed to the configured Discord
  webhooks (fire-and-forget, non-blocking). Configure via:
    DISCORD_WEBHOOK_SIGNALS, DISCORD_WEBHOOK_EXECUTIONS, DISCORD_WEBHOOK_STATUS
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from scipy import stats as sp_stats

# FastAPI is optional - the module works without it for backtesting
try:
    from fastapi import APIRouter, HTTPException, Depends, Request
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from regime_platform.auth import get_authed_user, require_write, require_admin, TokenData
from regime_platform.middleware.rate_limit import limiter, RATE_LIMIT_WRITE
from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline
from renko.brick_engine import BrickDirection
from renko.redis_cache import renko_cache
from renko.trading_heartbeat import trading_heartbeat

# Discord & Telegram notifications (graceful import - not required for pipeline to work)
try:
    from notifications.discord import discord_notifier, AlertLevel
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

try:
    from notifications.telegram import telegram_notifier
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

log = logging.getLogger(__name__)


# ── Numpy sanitization ────────────────────────────────────────────
# Pydantic can't serialize numpy.bool_, numpy.int64, numpy.float64, etc.
# Recursively convert all numpy types to native Python before returning.

def _sanitize_numpy(obj: Any) -> Any:
    """Recursively convert numpy types to native Python types for JSON serialization."""
    import numpy as np

    if isinstance(obj, dict):
        return {k: _sanitize_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return _sanitize_numpy(obj.tolist())
    return obj


# ── Pydantic models ───────────────────────────────────────────────


class TickInput(BaseModel):
    """Single tick input."""
    price: float = Field(..., description="Latest trade price")
    timestamp: Optional[float] = Field(None, description="Unix timestamp")
    symbol: Optional[str] = Field(None, description="Override symbol")


class BatchTickInput(BaseModel):
    """Batch of tick inputs for backtest / warm-up."""
    prices: List[float] = Field(..., description="Price series")
    timestamps: Optional[List[float]] = Field(None, description="Timestamps")
    regimes: Optional[List[str]] = Field(None, description="Pre-computed regime labels")
    symbol: Optional[str] = Field(None, description="Override symbol (defaults to query param)")


class WarmupRequest(BaseModel):
    """Warmup request with mode selection and response options."""
    symbol: str = Field("SPY", description="Trading symbol")
    period: str = Field("6mo", description="Yahoo Finance period: 1mo, 3mo, 6mo, 1y, 2y, 5y, max")
    mode: str = Field("auto", description="Warmup mode: full (always rebuild), incremental (append new data), auto (incremental if warm, full if cold)")
    include_state: bool = Field(True, description="Include full pipeline data in response (bricks, classified, trades, signals, stats)")


class ConfigInput(BaseModel):
    """Pipeline configuration."""
    brick_size: float = Field(0.50, description="Fixed brick size in dollars")
    brick_size_mode: str = Field("fixed", description="fixed | atr | dynamic")
    reversal_bricks: int = Field(2, description="Bricks needed for reversal")
    bull_trigger_n: int = Field(3, description="N consecutive HH/HL for bull trigger")
    bear_trigger_n: int = Field(3, description="N consecutive LL/LH for bear trigger")
    sl_bricks: int = Field(3, description="Stop-loss in brick units")
    tp_bricks: int = Field(5, description="Take-profit in brick units")
    trailing_stop: bool = Field(True, description="Enable trailing stop")
    trail_after_bricks: int = Field(3, description="Activate trail after N bricks profit")
    trail_distance_bricks: int = Field(2, description="Trail distance in bricks")
    max_trades_per_session: int = Field(15, description="Max trades per session")
    max_daily_loss_bricks: float = Field(10.0, description="Max daily loss in bricks")
    max_consecutive_losses: int = Field(3, description="Pause after N consecutive losses")
    cooldown_seconds: float = Field(30.0, description="Min seconds between trades")
    regime_gate: bool = Field(True, description="Only trade with HMM regime alignment")
    symbol: str = Field("SPY", description="Trading symbol")


class RegimeInput(BaseModel):
    """HMM regime update."""
    regime: str = Field(..., description="Current regime name")


class EquityInput(BaseModel):
    """Account equity update."""
    equity: float = Field(..., description="Current account equity")


class BacktestRequest(BaseModel):
    """Enhanced backtest request with Phase 2-7 parameters."""
    prices: List[float] = Field(..., description="Historical price series")
    timestamps: Optional[List[float]] = Field(None, description="Matching timestamps")
    regimes: Optional[List[str]] = Field(None, description="Pre-computed regime labels")
    symbol: Optional[str] = Field(None, description="Trading symbol")
    # Phase 2: Transaction costs
    slippage_bps: float = Field(2.0, description="Slippage in basis points")
    commission_bps: float = Field(5.0, description="Commission in basis points")
    spread_bps: float = Field(1.0, description="Bid-ask spread in basis points")
    # Phase 3: Execution realism
    sl_bricks: int = Field(3, description="Stop-loss in brick units")
    tp_bricks: int = Field(5, description="Take-profit in brick units")
    oco_priority: str = Field("sl_first", description="OCO priority: sl_first or tp_first")
    # Phase 7: Execution modeling
    market_impact_mode: str = Field("none", description="none or almgren_chriss")
    adv_shares: float = Field(10_000_000, description="Average daily volume in shares")
    fill_probability_mode: str = Field("always_fill", description="always_fill or realistic")
    borrow_rate_bps: float = Field(50.0, description="Annualized short borrow cost in bps")
    margin_rate_bps: float = Field(150.0, description="Annualized margin rate in bps")
    is_hard_to_borrow: bool = Field(False, description="Whether stock is hard-to-borrow")
    dividend_yield_bps: float = Field(200.0, description="Annualized dividend yield in bps")
    initial_capital: float = Field(100_000.0, description="Starting capital")
    # Brick engine
    brick_size: float = Field(0.50, description="Fixed brick size in dollars")
    # Phase 6: Statistical rigor
    n_trials: int = Field(1, description="Number of strategy trials attempted (for DSR)")
    confidence_level: float = Field(0.95, description="Confidence level for bootstrap CIs")


class BacktestStreamRequest(BaseModel):
    """Backtest with SSE streaming (Phase 1)."""
    prices: List[float] = Field(..., description="Historical price series")
    timestamps: Optional[List[float]] = Field(None, description="Matching timestamps")
    regimes: Optional[List[str]] = Field(None, description="Pre-computed regime labels")
    symbol: Optional[str] = Field(None, description="Trading symbol")
    chunk_size: int = Field(150, description="Ticks per chunk")
    # Phase 2+7 params (same as BacktestRequest)
    slippage_bps: float = Field(2.0)
    commission_bps: float = Field(5.0)
    spread_bps: float = Field(1.0)
    sl_bricks: int = Field(3)
    tp_bricks: int = Field(5)
    oco_priority: str = Field("sl_first")
    market_impact_mode: str = Field("none")
    adv_shares: float = Field(10_000_000)
    fill_probability_mode: str = Field("always_fill")
    borrow_rate_bps: float = Field(50.0)
    margin_rate_bps: float = Field(150.0)
    brick_size: float = Field(0.50)


class OptimizationRequest(BaseModel):
    """Parameter sweep optimization with statistical rigor."""
    prices: List[float] = Field(..., description="Historical price series")
    timestamps: Optional[List[float]] = Field(None)
    regimes: Optional[List[str]] = Field(None)
    symbol: Optional[str] = Field(None)
    # Sweep ranges
    brick_sizes: List[float] = Field([0.25, 0.50, 1.00], description="Brick sizes to test")
    sl_bricks_range: List[int] = Field([2, 3, 4], description="SL values to test")
    tp_bricks_range: List[int] = Field([4, 5, 6], description="TP values to test")
    # Risk-reward filter
    min_rr: float = Field(0.0, description="Minimum risk-reward ratio (tp_bricks / sl_bricks). Combos below this are skipped. Default 0.0 = no filter.")
    # Phase 2+7 params
    slippage_bps: float = Field(2.0)
    commission_bps: float = Field(5.0)
    spread_bps: float = Field(1.0)
    market_impact_mode: str = Field("none")
    adv_shares: float = Field(10_000_000)
    fill_probability_mode: str = Field("always_fill")
    borrow_rate_bps: float = Field(50.0)
    margin_rate_bps: float = Field(150.0)
    # Phase 6: Statistical rigor
    confidence_level: float = Field(0.95)
    multiple_testing_alpha: float = Field(0.05, description="Alpha for multiple testing correction")


class StatisticalRigorRequest(BaseModel):
    """Run statistical rigor tests on existing backtest results."""
    trade_pnls: List[float] = Field(..., description="Trade P&L series")
    observed_sharpe: float = Field(..., description="Observed Sharpe ratio")
    n_trials: int = Field(1, description="Number of strategy trials")
    confidence_level: float = Field(0.95)
    strategy_returns: Optional[List[List[float]]] = Field(None, description="Returns matrix for significance tests")
    benchmark_returns: Optional[List[float]] = Field(None, description="Benchmark returns")


class ExecutionModelRequest(BaseModel):
    """Compute execution modeling costs for a given trade."""
    order_shares: float = Field(..., description="Number of shares")
    avg_price: float = Field(..., description="Average share price")
    adv_shares: float = Field(10_000_000)
    sigma_daily: float = Field(0.02)
    order_type: str = Field("limit")
    distance_from_mid_pct: float = Field(0.0)
    position_notional: float = Field(0.0)
    holding_days: float = Field(10.0)
    direction: str = Field("long")
    is_hard_to_borrow: bool = Field(False)
    borrow_rate_bps: float = Field(50.0)
    margin_rate_bps: float = Field(150.0)
    dividend_yield_bps: float = Field(200.0)


class SizingModelRequest(BaseModel):
    """Set or query the position sizing model for a pipeline."""
    sizing_mode: str = Field("kelly", description="Sizing mode: 'kelly' or 'masaniello'")
    # Masaniello-specific overrides (only used when sizing_mode=masaniello)
    base_risk: Optional[float] = Field(None, description="Base risk fraction β (default 0.005)")
    min_risk: Optional[float] = Field(None, description="Hard floor risk fraction (default 0.0025)")
    max_risk: Optional[float] = Field(None, description="Hard cap risk fraction (default 0.010)")
    min_prob: Optional[float] = Field(None, description="Min win probability to trade (default 0.50)")
    min_rr: Optional[float] = Field(None, description="Min reward/risk ratio (default 2.50)")
    max_drawdown: Optional[float] = Field(None, description="Max strategy drawdown fraction (default 0.10)")
    regime_floor: Optional[float] = Field(None, description="Min regime quality to trade (default 0.50)")
    use_kelly_overlay: Optional[bool] = Field(None, description="Cap Masaniello at Kelly bound")
    batch_size: Optional[int] = Field(None, description="N trades per batch (default 5)")
    target_wins: Optional[int] = Field(None, description="W target wins per batch (default 3)")


class EVComputeRequest(BaseModel):
    """Full EV computation request with blended P_win from regime + imbalance + Markov."""
    prices: List[float] = Field(..., description="Historical price series for Markov chain fitting")
    symbol: str = Field("SPY", description="Trading symbol")
    brick_size: float = Field(0.50, description="Brick size in dollars")
    sl_bricks: int = Field(3, description="Stop-loss in brick units")
    tp_bricks: int = Field(5, description="Take-profit in brick units")
    direction: str = Field("long", description="Trade direction: 'long' or 'short'")
    # Optional signal sources (if not provided, computed from prices)
    trend_probs: Optional[Dict[str, float]] = Field(None, description="HMM trend posterior probabilities (e.g. {'bear': 0.1, 'neutral': 0.3, 'bull': 0.6})")
    base_kelly: float = Field(0.0, description="Base Kelly fraction for EV scaling")
    markov_window: int = Field(200, description="Rolling window for Markov chain observations")


class EVComputeResponse(BaseModel):
    """Full EV computation response with blended P_win breakdown and execution strategy."""
    p_win: float
    p_regime: float
    p_pattern: float  # v5: was p_ext_signal in v4 (microstructure deprecated from EV)
    p_markov: float
    ev: float
    ev_per_dollar: float
    rr: float
    profit: float
    stake: float
    allowed: bool
    aggression: str
    ev_scale: float
    markov_multiplier: float = 1.0  # v4: Markov persistence → sizing multiplier
    order_type: str
    patience_bars: int
    markov_current_state: str
    markov_n_observations: int


# ── DEPRECATED backward-compat Renko* API (pre-v7.3, 2026-08-05) ────────
# The v7.3 refactor (Redis usage optimization) renamed the Renko backtest
# request/response models to `BacktestRequest` / `OptimizationRequest` /
# `StatisticalRigorRequest` etc. and removed the walk-forward / Monte-Carlo
# pipeline helpers. Several legacy tests (test_renko_backtest.py,
# test_data_quality.py::TestRouterDataQuality, test_backtest_cache.py::
# TestCachedField) still import the OLD `Renko*` names. These deprecated
# aliases keep that surface importable so the suite collects. New code must
# use the current models — do NOT extend these.

class RenkoBacktestRequest(BaseModel):
    """DEPRECATED (v7.3) — use `BacktestRequest`. Kept for legacy tests.

    Restored in full (incl. Phase-4/5 fields) because the legacy tests
    assert on these exact defaults (symbol='SPY', regime_gate=True,
    universe_mode='current_constituents', ...) — a bare subclass of
    BacktestRequest would not satisfy them.
    """
    prices: List[float] = Field(..., min_length=50, description="Historical price series (min 50 ticks).")
    symbol: str = Field(default="SPY", description="Trading symbol.")
    # Brick Engine
    brick_size: float = Field(default=0.50, gt=0, description="Fixed brick size in dollars.")
    brick_size_mode: str = Field(default="fixed", description="fixed | atr | dynamic")
    reversal_bricks: int = Field(default=2, ge=1, description="Bricks needed for reversal.")
    # Pattern Detector
    bull_trigger_n: int = Field(default=3, ge=1, description="N consecutive HH/HL for bull trigger.")
    bear_trigger_n: int = Field(default=3, ge=1, description="N consecutive LL/LH for bear trigger.")
    # Risk Manager
    sl_bricks: int = Field(default=3, ge=1, description="Stop-loss in brick units.")
    tp_bricks: int = Field(default=5, ge=1, description="Take-profit in brick units.")
    trailing_stop: bool = Field(default=True, description="Enable trailing stop.")
    trail_after_bricks: int = Field(default=3, ge=1, description="Activate trail after N bricks profit.")
    trail_distance_bricks: int = Field(default=2, ge=1, description="Trail distance in bricks.")
    # Signal Filter
    max_trades_per_session: int = Field(default=15, ge=1, description="Max trades per session.")
    max_daily_loss_bricks: float = Field(default=10.0, gt=0, description="Max daily loss in bricks.")
    max_consecutive_losses: int = Field(default=3, ge=1, description="Pause after N consecutive losses.")
    cooldown_seconds: float = Field(default=30.0, ge=0, description="Min seconds between trades.")
    regime_gate: bool = Field(default=True, description="Only trade with HMM regime alignment.")
    # Transaction Costs
    slippage_bps: float = Field(default=2.0, ge=0, description="Slippage in basis points (adverse fill).")
    commission_bps: float = Field(default=5.0, ge=0, description="Commission in basis points of position value.")
    spread_bps: float = Field(default=1.0, ge=0, description="Bid-ask spread in basis points (half-spread each side).")
    # Execution Realism
    oco_priority: str = Field(default="sl_first", description="OCO resolution: sl_first | tp_first | worst_case.")
    # Capital / Dollar P&L (Phase 4A)
    initial_capital: float = Field(default=100000.0, gt=0, description="Starting capital for dollar P&L.")
    # Optional overrides
    timestamps: Optional[List[float]] = Field(default=None, description="Matching timestamps for prices.")
    regimes: Optional[List[str]] = Field(default=None, description="Pre-computed regime labels aligned with prices.")
    signal_confidence_min: Optional[float] = Field(default=None, ge=0, le=1, description="Min signal confidence to trade.")
    # ── Data Quality (Phase 5) ─────────────────────────────────────────
    universe_mode: str = Field(default="current_constituents", description="Universe filtering: current_constituents (default) | pit_constituents.")
    index_name: Optional[str] = Field(default=None, description="Index for PIT filtering (e.g. 'sp500').")
    price_adjustment: str = Field(default="raw", description="Price adjustment mode: raw | split_adjusted | fully_adjusted.")
    look_ahead_audit: bool = Field(default=False, description="Enable look-ahead bias audit.")


class RenkoBacktestResponse(BaseModel):
    """DEPRECATED (v7.3) — response contract used by legacy backtest tests."""
    symbol: str
    total_ticks: int
    total_bricks: int
    config_used: Dict[str, Any] = Field(default_factory=dict)
    stats: Dict[str, Any] = Field(default_factory=dict)
    trades: List[Dict[str, Any]] = Field(default_factory=list)
    cached: bool = Field(default=False, description="True if served from cache (1h TTL).")
    # ── Data Quality (Phase 5) ─────────────────────────────────────────
    data_hash: Optional[str] = Field(default=None, description="SHA-256 hash of input price data.")
    data_source: Dict[str, Any] = Field(default_factory=dict, description="Provenance metadata.")
    survivorship_bias: Optional[Dict[str, Any]] = Field(default=None, description="Survivorship bias warning/info.")
    price_adjustments_applied: List[Dict[str, Any]] = Field(default_factory=list, description="Corporate action adjustments applied.")
    data_quality_warnings: List[str] = Field(default_factory=list, description="Data quality warnings (NaN, inf, jumps, etc.).")
    look_ahead_audit_result: Optional[Dict[str, Any]] = Field(default=None, description="Look-ahead bias audit results.")


class RenkoCompareConfig(BaseModel):
    """DEPRECATED (v7.3) — use `OptimizationRequest`. Kept for legacy tests."""
    label: str = Field(..., description="Label for this config (e.g. 'aggressive').")
    brick_size: float = Field(default=0.50, gt=0)
    sl_bricks: int = Field(default=3, ge=1)
    tp_bricks: int = Field(default=5, ge=1)
    trailing_stop: bool = Field(default=True)
    trail_after_bricks: int = Field(default=3, ge=1)
    trail_distance_bricks: int = Field(default=2, ge=1)
    bull_trigger_n: int = Field(default=3, ge=1)
    bear_trigger_n: int = Field(default=3, ge=1)
    regime_gate: bool = Field(default=True)
    max_trades_per_session: int = Field(default=15, ge=1)
    max_daily_loss_bricks: float = Field(default=10.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    slippage_bps: float = Field(default=2.0, ge=0)
    commission_bps: float = Field(default=5.0, ge=0)
    spread_bps: float = Field(default=1.0, ge=0)
    oco_priority: str = Field(default="sl_first")


class RenkoBacktestCompareRequest(BaseModel):
    """DEPRECATED (v7.3) — legacy compare request. Kept for tests."""
    prices: List[float] = Field(..., min_length=50)
    symbol: str = Field(default="SPY")
    configs: List[RenkoCompareConfig] = Field(..., min_length=2, max_length=10)
    timestamps: Optional[List[float]] = Field(default=None)
    regimes: Optional[List[str]] = Field(default=None)


class RenkoBacktestCompareResponse(BaseModel):
    """DEPRECATED (v7.3) — legacy compare response. Kept for tests."""
    comparisons: List[RenkoBacktestResponse]
    diff: Dict[str, Any] = Field(default_factory=dict)


class RenkoBacktestOptimizeRequest(BaseModel):
    """DEPRECATED (v7.3) — use `OptimizationRequest`. Kept for tests."""
    prices: List[float] = Field(..., min_length=50)
    symbol: str = Field(default="SPY")
    param_grid: Dict[str, List] = Field(...)
    brick_size: float = Field(default=0.50, gt=0)
    brick_size_mode: str = Field(default="fixed")
    reversal_bricks: int = Field(default=2, ge=1)
    bull_trigger_n: int = Field(default=3, ge=1)
    bear_trigger_n: int = Field(default=3, ge=1)
    sl_bricks: int = Field(default=3, ge=1)
    tp_bricks: int = Field(default=5, ge=1)
    trailing_stop: bool = Field(default=True)
    trail_after_bricks: int = Field(default=3, ge=1)
    trail_distance_bricks: int = Field(default=2, ge=1)
    max_trades_per_session: int = Field(default=15, ge=1)
    max_daily_loss_bricks: float = Field(default=10.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    regime_gate: bool = Field(default=True)
    timestamps: Optional[List[float]] = Field(default=None)
    regimes: Optional[List[str]] = Field(default=None)
    slippage_bps: float = Field(default=2.0, ge=0)
    commission_bps: float = Field(default=5.0, ge=0)
    spread_bps: float = Field(default=1.0, ge=0)
    oco_priority: str = Field(default="sl_first")


class RenkoBacktestOptimizeResponse(BaseModel):
    """DEPRECATED (v7.3) — legacy optimize response. Kept for tests."""
    results: List[Dict[str, Any]] = Field(...)
    best_by_sharpe: Optional[Dict[str, Any]] = Field(default=None)
    best_by_return: Optional[Dict[str, Any]] = Field(default=None)
    n_combinations: int = Field(...)


class RenkoWalkForwardRequest(BaseModel):
    """DEPRECATED (v7.3) — walk-forward pipeline methods were removed.
    Kept only so legacy model-validation tests can import it."""
    prices: List[float] = Field(..., min_length=200)
    symbol: str = Field(default="SPY")
    train_window: int = Field(default=500, ge=100)
    test_window: int = Field(default=250, ge=50)
    min_trades_for_stats: int = Field(default=5, ge=1)


class RenkoWalkForwardResponse(BaseModel):
    """DEPRECATED (v7.3) — see RenkoWalkForwardRequest."""
    windows: List[Dict[str, Any]] = Field(default_factory=list)
    aggregate: Dict[str, Any] = Field(default_factory=dict)
    config_used: Dict[str, Any] = Field(default_factory=dict)
    total_ticks: int = 0


class RenkoMonteCarloRequest(BaseModel):
    """DEPRECATED (v7.3) — Monte-Carlo pipeline methods were removed.
    Kept only so legacy model-validation tests can import it."""
    prices: List[float] = Field(..., min_length=100)
    symbol: str = Field(default="SPY")
    n_simulations: int = Field(default=1000, ge=100, le=10000)


class RenkoMonteCarloResponse(BaseModel):
    """DEPRECATED (v7.3) — see RenkoMonteCarloRequest."""
    original: Dict[str, Any] = Field(default_factory=dict)
    confidence_bands: Dict[str, Any] = Field(default_factory=dict)
    p_profitable: float = 0.0
    p_beat_original: float = 0.0
    n_trades: int = 0
    simulation_count: int = 0


def _build_renko_config(req: "RenkoBacktestRequest") -> "RenkoConfig":
    """DEPRECATED (v7.3) — legacy helper used by test_renko_backtest.py.
    Builds a RenkoConfig from the legacy backtest request (now an alias of
    the current BacktestRequest)."""
    from renko.config import RenkoConfig as _RenkoConfig
    return _RenkoConfig(
        brick_size=req.brick_size,
        brick_size_mode=req.brick_size_mode,
        reversal_bricks=req.reversal_bricks,
        bull_trigger_n=req.bull_trigger_n,
        bear_trigger_n=req.bear_trigger_n,
        sl_bricks=req.sl_bricks,
        tp_bricks=req.tp_bricks,
        trailing_stop=req.trailing_stop,
        trail_after_bricks=req.trail_after_bricks,
        trail_distance_bricks=req.trail_distance_bricks,
        max_trades_per_session=req.max_trades_per_session,
        max_daily_loss_bricks=req.max_daily_loss_bricks,
        max_consecutive_losses=req.max_consecutive_losses,
        cooldown_seconds=req.cooldown_seconds,
        regime_gate=req.regime_gate,
        slippage_bps=req.slippage_bps,
        commission_bps=req.commission_bps,
        spread_bps=req.spread_bps,
        oco_priority=req.oco_priority,
        initial_capital=req.initial_capital,
        symbol=req.symbol,
    )


def _run_isolated_backtest(
    prices: List[float],
    config: "RenkoConfig",
    timestamps: Optional[List[float]] = None,
    regimes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """DEPRECATED (v7.3) — legacy helper used by test_renko_backtest.py.
    Runs a backtest in an isolated pipeline (never touches live state)."""
    from renko.pipeline import RenkoPipeline as _RenkoPipeline
    pipeline = _RenkoPipeline(config)
    return pipeline.backtest(
        prices=prices,
        timestamps=timestamps,
        regimes=regimes,
    )


# ── Pipeline registry ─────────────────────────────────────────────

# In-memory registry of active pipelines - keyed by composite user_id:symbol
# For MCP agent use (single admin), key is just "symbol" (no user_id).
# For multi-user, key is "user_id:symbol" for isolation.
_pipelines: Dict[str, RenkoPipeline] = {}


def _pipeline_key(symbol: str, user_id: Optional[str] = None) -> str:
    """Build a stable registry key for a pipeline.

    Session Key Stability: uses ``user_id:symbol`` when user_id is
    available, falling back to just ``symbol`` for MCP agent use
    (single-admin mode). This prevents cross-user state leakage
    and ensures session continuity across restarts.

    Parameters
    ----------
    symbol : str
        Trading symbol (e.g. "SPY")
    user_id : str, optional
        Clerk user ID from auth token. None for MCP agent / admin.
    """
    if user_id:
        return f"{user_id}:{symbol}"
    return symbol


def _get_pipeline(symbol: str = "SPY", config: Optional[RenkoConfig] = None) -> RenkoPipeline:
    """Get or create a pipeline for a symbol. Uses hardcoded defaults (sync)."""
    key = _pipeline_key(symbol)
    if key not in _pipelines:
        if config is None:
            config = RenkoConfig(symbol=symbol)
        _pipelines[key] = RenkoPipeline(config)
    return _pipelines[key]


async def _get_pipeline_async(
    symbol: str = "SPY",
    config_overrides: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> RenkoPipeline:
    """Get or create a pipeline for a symbol. Uses runtime config (async).

    HydrateOnRequest: attempts Supabase snapshot restore whenever the
    pipeline is empty (brick_count == 0), not just on first access.
    This ensures MCP agents NEVER see a void state - even after a
    server restart that leaves an empty entry in _pipelines.

    Restore hierarchy:
      1. If pipeline exists and has bricks -> return immediately
      2. If pipeline is empty or missing -> try Supabase restore
      3. If restore fails -> create fresh pipeline from runtime config

    Session Key Stability: uses ``user_id:symbol`` composite key
    when user_id is provided, ensuring user isolation.
    """
    key = _pipeline_key(symbol, user_id)

    if key in _pipelines:
        pipeline = _pipelines[key]
        # ── HydrateOnRequest: re-attempt restore if pipeline is empty ──
        if pipeline.brick_engine.brick_count > 0:
            return pipeline
        # Pipeline exists but has 0 bricks - try to hydrate from Supabase
        log.info(
            "HydrateOnRequest: pipeline %s is empty (brick_count=0), "
            "attempting Supabase restore...",
            key,
        )
        restored = await _restore_pipeline_snapshot(symbol, user_id=user_id)
        if restored and restored.brick_engine.brick_count > 0:
            _pipelines[key] = restored
            log.info(
                "HydrateOnRequest: restored %d bricks for %s",
                restored.brick_engine.brick_count, key,
            )
            return restored
        # Restore failed or returned empty - return the existing empty pipeline
        return pipeline

    # First access for this key - try Supabase restore
    restored = await _restore_pipeline_snapshot(symbol, user_id=user_id)
    if restored:
        _pipelines[key] = restored
        return restored

    # No snapshot available - create fresh pipeline from runtime config
    # Try trading:config:{symbol} Redis cache first to skip Supabase query
    cached_config = await trading_heartbeat.get_config(symbol)
    if cached_config and not config_overrides:
        try:
            # Build RenkoConfig from cached Redis hash (sub-ms vs ~100ms Supabase)
            config_kwargs = {"symbol": symbol}
            _defaults = RenkoConfig()
            for field_name in RenkoConfig.__dataclass_fields__:
                if field_name in cached_config and field_name != "config_ts":
                    field_type = type(getattr(_defaults, field_name))
                    raw = cached_config[field_name]
                    try:
                        if field_type == bool:
                            config_kwargs[field_name] = raw.lower() == "true"
                        elif field_type == int:
                            config_kwargs[field_name] = int(float(raw))
                        elif field_type == float:
                            config_kwargs[field_name] = float(raw)
                        else:
                            config_kwargs[field_name] = raw
                    except (ValueError, TypeError):
                        pass  # Skip fields we can't parse
            config = RenkoConfig(**config_kwargs)
            log.info("Created pipeline for %s from trading:config cache", symbol)
        except Exception as e:
            log.debug("Failed to create config from cache for %s: %s — falling back", symbol, e)
            config = await RenkoConfig.from_runtime_config(
                overrides={"symbol": symbol, **(config_overrides or {})}
            )
    else:
        config = await RenkoConfig.from_runtime_config(
            overrides={"symbol": symbol, **(config_overrides or {})}
        )
    _pipelines[key] = RenkoPipeline(config)
    return _pipelines[key]


async def _get_pipeline_with_restore(
    symbol: str = "SPY",
    user_id: Optional[str] = None,
) -> RenkoPipeline:
    """Get or create a pipeline for a symbol, restoring from Supabase snapshot if available.

    Alias for ``_get_pipeline_async`` which now auto-restores on empty state.
    Kept for readability on write-path endpoints.
    """
    return await _get_pipeline_async(symbol, user_id=user_id)


async def _on_config_changed(key: str, entry) -> None:
    """
    Callback registered with runtime_config.register_on_change().

    When a renko.* config key changes, invalidate all cached pipelines
    so the next request creates fresh pipelines with updated config.
    This ensures runtime config changes take effect immediately without
    requiring a server restart.
    
    Also invalidates trading:config:{symbol} Redis caches so the next
    pipeline creation picks up the new config from Supabase.
    """
    if key.startswith("renko."):
        invalidated = list(_pipelines.keys())
        _pipelines.clear()
        # Invalidate all config caches (they're now stale)
        if trading_heartbeat.enabled:
            for pipeline_key in invalidated:
                # pipeline_key may be "symbol" or "user_id:symbol"
                sym = pipeline_key.split(":")[-1] if ":" in pipeline_key else pipeline_key
                asyncio.create_task(trading_heartbeat.invalidate_config(sym))
        if invalidated:
            log.info(
                "Config %s changed - invalidated %d pipeline(s): %s",
                key, len(invalidated), ", ".join(invalidated),
            )


# ── Router ─────────────────────────────────────────────────────────


def _build_full_state(pipeline: RenkoPipeline) -> Dict[str, Any]:
    """Build a full pipeline state dict for warmup responses.

    Includes bricks, classified, signals, trades, stats, and readiness
    so the frontend can render the Renko page without making 5+ extra
    GET calls after a warmup.
    """
    bricks = list(pipeline.brick_engine.history)
    classified = list(pipeline.swing_classifier.classified_history)
    signals = pipeline.pattern_detector.signals
    trades = list(pipeline.journal.trades)

    return {
        "bricks": [b.to_dict() for b in bricks[-500:]],  # Last 500 bricks
        "classified": [c.to_dict() for c in classified[-500:]],
        "signals": [s.to_dict() for s in signals[-100:]],
        "trades": [t.to_dict() for t in trades[-100:]],
        "stats": pipeline.get_stats(),
        "readiness": pipeline.readiness,
        "state": pipeline.get_state().to_dict(),
    }


if FASTAPI_AVAILABLE:
    # Auth imports - conditional since regime_platform may not be on sys.path
    try:
        from regime_platform.auth.jwt_auth import get_authed_user, require_write, require_admin, TokenData
        AUTH_AVAILABLE = True
    except ImportError:
        # Fallback: define no-op dependencies for standalone mode
        AUTH_AVAILABLE = False
        async def _no_auth(): return None
        get_authed_user = _no_auth
        require_write = _no_auth
        require_admin = _no_auth
        TokenData = None

    renko_router = APIRouter(prefix="/renko", tags=["renko"])

    # ── Health-check (no auth required) ──────────────────────────────

    @renko_router.get("/health")
    async def renko_health() -> Dict[str, Any]:
        """
        Diagnostic endpoint - tests pipeline creation, runtime_config,
        and Supabase connectivity. No auth required.
        """
        import traceback
        checks = {"status": "ok", "errors": []}

        # 1. Test runtime_config
        try:
            from regime_platform.core.runtime_config import runtime_config
            await runtime_config._ensure_cache()
            checks["runtime_config"] = {
                "available": True,
                "cache_entries": len(runtime_config._cache),
                "db_available": runtime_config._db_available,
            }
        except Exception as e:
            checks["runtime_config"] = {"available": False, "error": str(e)}
            checks["errors"].append(f"runtime_config: {e}")
            checks["status"] = "degraded"

        # 2. Test RenkoConfig.from_runtime_config
        try:
            config = await RenkoConfig.from_runtime_config(overrides={"symbol": "SPY"})
            checks["config_creation"] = {
                "ok": True,
                "brick_size": config.brick_size,
                "symbol": config.symbol,
                "brick_size_mode": config.brick_size_mode.value,
            }
        except Exception as e:
            checks["config_creation"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            checks["errors"].append(f"config_creation: {e}")
            checks["status"] = "broken"

        # 3. Test pipeline creation
        try:
            config = RenkoConfig(symbol="SPY")
            pipeline = RenkoPipeline(config)
            state = pipeline.get_state()
            checks["pipeline_creation"] = {
                "ok": True,
                "brick_count": state.brick_count,
            }
        except Exception as e:
            checks["pipeline_creation"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            checks["errors"].append(f"pipeline_creation: {e}")
            checks["status"] = "broken"

        # 4. Test Supabase snapshot table
        try:
            from regime_platform.core.settings import settings
            if settings.SUPABASE_URL and settings.SUPABASE_KEY:
                import httpx
                url = f"{settings.SUPABASE_URL}/rest/v1/nt_renko_snapshot?select=symbol&limit=1"
                headers = {
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                }
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    checks["supabase_snapshot"] = {
                        "ok": resp.status_code in (200, 201, 204, 404),
                        "status_code": resp.status_code,
                    }
                    if resp.status_code == 404:
                        checks["supabase_snapshot"]["note"] = "Table may not exist"
            else:
                checks["supabase_snapshot"] = {"ok": False, "note": "SUPABASE_URL/KEY not configured"}
        except Exception as e:
            checks["supabase_snapshot"] = {"ok": False, "error": str(e)}
            checks["errors"].append(f"supabase_snapshot: {e}")

        # 5. Test Redis L1 cache
        try:
            checks["redis_l1_cache"] = {
                "enabled": renko_cache.enabled,
            }
            if renko_cache.enabled:
                symbols = await renko_cache.list_symbols()
                checks["redis_l1_cache"]["cached_symbols"] = len(symbols)
                checks["redis_l1_cache"]["symbols"] = symbols[:5]  # Show first 5
        except Exception as e:
            checks["redis_l1_cache"] = {"enabled": renko_cache.enabled, "error": str(e)}

        # 6. Test _get_pipeline_async (the actual call path for GET endpoints)
        try:
            pipeline = await _get_pipeline_async("SPY")
            checks["pipeline_async"] = {
                "ok": True,
                "brick_count": pipeline.brick_engine.brick_count,
                "symbol": pipeline.config.symbol,
            }
        except Exception as e:
            checks["pipeline_async"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            checks["errors"].append(f"pipeline_async: {e}")
            checks["status"] = "broken"

        return checks

    @renko_router.get("/heartbeat")
    async def get_heartbeat(symbol: str = "SPY", user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Get instant pipeline state from trading:heartbeat:{symbol}.

        O(1) Redis HGETALL — no Supabase query, no pipeline hydration.
        Returns readiness level, brick count, last price, position state,
        and data freshness. Use this for:
          - Frontend initialization (skip 3-step waterfall)
          - MCP agent readiness checks (before trading)
          - Auto-warmup scheduler (find stale symbols)

        Falls back to in-memory pipeline state if Redis is unavailable.
        """
        # Try Redis heartbeat first (sub-ms)
        heartbeat = await trading_heartbeat.get(symbol)
        if heartbeat:
            # Parse numeric fields for convenience
            try:
                brick_count = int(heartbeat.get("brick_count", "0"))
            except (ValueError, TypeError):
                brick_count = 0
            try:
                last_price = float(heartbeat.get("last_price", "0"))
            except (ValueError, TypeError):
                last_price = 0.0
            try:
                snapshot_ts = float(heartbeat.get("snapshot_ts", "0"))
            except (ValueError, TypeError):
                snapshot_ts = 0.0
            try:
                last_tick_ts = float(heartbeat.get("last_tick_ts", "0"))
            except (ValueError, TypeError):
                last_tick_ts = 0.0
            try:
                last_brick_ts = float(heartbeat.get("last_brick_ts", "0"))
            except (ValueError, TypeError):
                last_brick_ts = 0.0

            age_seconds = (time.time() - snapshot_ts) if snapshot_ts > 0 else None
            tick_age_seconds = (time.time() - last_tick_ts) if last_tick_ts > 0 else None

            return {
                "available": True,
                "source": "redis",
                "symbol": symbol,
                "status": heartbeat.get("status", "cold"),
                "brick_count": brick_count,
                "last_price": last_price,
                "last_brick_ts": last_brick_ts,
                "last_tick_ts": last_tick_ts,
                "snapshot_ts": snapshot_ts,
                "has_position": heartbeat.get("has_position", "false") == "true",
                "warmup_mode": heartbeat.get("warmup_mode", ""),
                "source_mode": heartbeat.get("source", ""),
                "regime": heartbeat.get("regime", ""),
                "direction": heartbeat.get("direction", ""),
                "sizing_mode": heartbeat.get("sizing_mode", "kelly"),
                "equity": heartbeat.get("equity", "100000"),
                "age_seconds": round(age_seconds) if age_seconds is not None else None,
                "tick_age_seconds": round(tick_age_seconds) if tick_age_seconds is not None else None,
                "is_fresh": age_seconds is not None and age_seconds < 14400,
            }

        # Fallback: derive from in-memory pipeline
        pipeline = await _get_pipeline_async(symbol)
        readiness = pipeline.readiness
        return {
            "available": True,
            "source": "pipeline_fallback",
            "symbol": symbol,
            "status": readiness["level"],
            "brick_count": readiness["brick_count"],
            "last_price": pipeline.brick_engine.last_brick.close_price if pipeline.brick_engine.last_brick else None,
            "last_brick_ts": pipeline.brick_engine.last_brick.timestamp if pipeline.brick_engine.last_brick else None,
            "last_tick_ts": None,
            "snapshot_ts": None,
            "has_position": readiness["has_position"],
            "warmup_mode": "",
            "source_mode": "",
            "regime": readiness.get("regime"),
            "direction": (
                pipeline.brick_engine.current_direction.value
                if pipeline.brick_engine.current_direction else None
            ),
            "sizing_mode": readiness.get("sizing_mode", "kelly"),
            "equity": str(pipeline._current_equity),
            "age_seconds": None,
            "tick_age_seconds": None,
            "is_fresh": False,
        }

    @renko_router.get("/heartbeat/all")
    async def get_all_heartbeats(user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Get heartbeat status for all tracked symbols.

        Useful for the auto-warmup scheduler and dashboard overview.
        """
        symbols = await trading_heartbeat.list_symbols()
        results = {}
        for sym in symbols:
            hb = await trading_heartbeat.get(sym)
            if hb:
                results[sym] = {
                    "status": hb.get("status", "cold"),
                    "brick_count": int(hb.get("brick_count", "0")),
                    "has_position": hb.get("has_position", "false") == "true",
                    "direction": hb.get("direction", ""),
                    "regime": hb.get("regime", ""),
                }
        return {"symbols": results, "count": len(results)}

    @renko_router.post("/tick")
    @limiter.limit(RATE_LIMIT_WRITE)
    async def process_tick(request: Request, body: TickInput, user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Process a single tick through the Renko pipeline."""
        symbol = body.symbol or "SPY"
        pipeline = await _get_pipeline_with_restore(symbol)
        result = pipeline.process_tick(body.price, body.timestamp)

        # ── Wire executor: check for pending open/close ─────────────
        if not pipeline.config.dry_run and pipeline.config.live_enabled:
            # Check for pending position open
            pending_exec = pipeline.consume_pending_execution()
            if pending_exec:
                order_result = await _execute_signal(pending_exec, user, symbol)
                result["order_result"] = order_result

            # Check for pending position close
            pending_close = pipeline.consume_pending_close()
            if pending_close:
                close_result = await _close_pipeline_position(pending_close, user)
                result["close_result"] = close_result

        # ── Fire Discord notifications (non-blocking) ────────────
        _notify_tick_result(symbol, result, pipeline)

        # ── Auto-save on critical events (MCP agent safety) ────────
        # Save immediately when a position was opened or closed
        # (avoids orphaned trades on restart). v8.0: removed the
        # periodic 100th-tick checkpoint — L1 snapshots only on
        # meaningful state changes to reduce Redis write volume.
        has_position_event = (
            result.get("filtered") is not None
            or result.get("trade_result") is not None
        )
        if has_position_event:
            asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))

        # ── v8.0 REMOVED: per-tick trading heartbeat writes ──────
        # Heartbeat is now written by the SweepOrchestrator on LightningAI
        # every sweep cycle (liveness). The Python backend no longer
        # writes to trading:heartbeat:{symbol} on every tick.
        # The agent subscribes to agent:{symbol} for signals
        # and polls trading:heartbeat:{symbol} at 15-min cadence.
        #
        # if has_position_event:
        #     asyncio.create_task(trading_heartbeat.update_from_pipeline(symbol, pipeline))
        # else:
        #     asyncio.create_task(trading_heartbeat.update(
        #         symbol,
        #         last_price=body.price,
        #         last_tick_ts=time.time(),
        #         brick_count=pipeline.brick_engine.brick_count,
        #         has_position=pipeline.risk_manager.has_position,
        #     ))

        return result

    @renko_router.post("/tick/batch")
    @limiter.limit(RATE_LIMIT_WRITE)
    async def process_batch(request: Request, body: BatchTickInput, symbol: str = "SPY", user=Depends(require_write)) -> Dict[str, Any]:
        """Process a batch of ticks (backtest / warm-up).

        Atomic Commit: uses confirmed save (awaited) instead of
        fire-and-forget, ensuring MCP agents can rely on state
        being persisted before the response returns.
        """
        # Body symbol takes precedence over query param
        active_symbol = body.symbol or symbol
        user_id = getattr(user, 'sub', None) if user else None
        pipeline = await _get_pipeline_with_restore(active_symbol, user_id=user_id)
        result = pipeline.backtest(
            prices=body.prices,
            timestamps=body.timestamps,
            regimes=body.regimes,
        )

        # ── Fire Discord status notification for batch completion ─
        _notify_batch_complete(active_symbol, result)

        # ── Atomic Commit: confirmed save (awaited, not fire-and-forget) ──
        # MCP agents need to know state is durable before proceeding
        saved = await _save_pipeline_snapshot_confirmed(active_symbol, pipeline)
        result["_persistence"] = {
            "saved": saved,
            "store": "L1+L2" if renko_cache.enabled else "L2-only",
        }

        return result

    @renko_router.get("/state")
    async def get_state(symbol: str = "SPY", user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Get current pipeline state with readiness assessment.

        MCP agents should check ``readiness.level`` before trading:
          - ``cold``: needs warmup (brick_count == 0)
          - ``warming``: insufficient data (brick_count < 50)
          - ``warm``: signals active but limited history
          - ``hot``: full detection + journal, position already open
          - ``live_ready``: ready for new entries
        """
        try:
            pipeline = await _get_pipeline_async(symbol)
            state = pipeline.get_state().to_dict()
            state["readiness"] = pipeline.readiness
            return state
        except Exception as e:
            log.error(f"get_state failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/stats")
    async def get_stats(symbol: str = "SPY", user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Get comprehensive pipeline statistics."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            return _sanitize_numpy(pipeline.get_stats())
        except Exception as e:
            log.error(f"get_stats failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/bricks")
    async def get_bricks(symbol: str = "SPY", last_n: int = 100, user=Depends(get_authed_user)) -> List[Dict]:
        """Get recent bricks."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            bricks = list(pipeline.brick_engine.history)
            return [b.to_dict() for b in bricks[-last_n:]]
        except Exception as e:
            log.error(f"get_bricks failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/classified")
    async def get_classified(symbol: str = "SPY", last_n: int = 100, user=Depends(get_authed_user)) -> List[Dict]:
        """Get recent classified bricks."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            classified = list(pipeline.swing_classifier.classified_history)
            return [c.to_dict() for c in classified[-last_n:]]
        except Exception as e:
            log.error(f"get_classified failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/signals")
    async def get_signals(symbol: str = "SPY", last_n: int = 50, user=Depends(get_authed_user)) -> List[Dict]:
        """Get recent pattern signals."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            signals = pipeline.pattern_detector.signals
            return [s.to_dict() for s in signals[-last_n:]]
        except Exception as e:
            log.error(f"get_signals failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/trades")
    async def get_trades(symbol: str = "SPY", last_n: int = 50, user=Depends(get_authed_user)) -> List[Dict]:
        """Get recent trade records."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            trades = list(pipeline.journal.trades)
            return [t.to_dict() for t in trades[-last_n:]]
        except Exception as e:
            log.error(f"get_trades failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.get("/swing-points")
    async def get_swing_points(symbol: str = "SPY", last_n: int = 50, user=Depends(get_authed_user)) -> List[Dict]:
        """Get detected swing points."""
        try:
            pipeline = await _get_pipeline_async(symbol)
            points = pipeline.swing_classifier.swing_points
            return [p.to_dict() for p in points[-last_n:]]
        except Exception as e:
            log.error(f"get_swing_points failed for {symbol}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Pipeline init failed: {e}")

    @renko_router.post("/regime")
    async def set_regime(body: RegimeInput, symbol: str = "SPY", user=Depends(require_admin)) -> Dict:
        """Update the current HMM regime."""
        pipeline = await _get_pipeline_async(symbol)
        pipeline.set_regime(body.regime)
        # Auto-save after regime change (critical for MCP agent state sync)
        asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))
        return {"status": "ok", "regime": body.regime}

    @renko_router.post("/equity")
    async def set_equity(body: EquityInput, symbol: str = "SPY", user=Depends(require_admin)) -> Dict:
        """Update the current account equity."""
        pipeline = await _get_pipeline_async(symbol)
        pipeline.set_equity(body.equity)
        # Auto-save after equity change (critical for MCP agent state sync)
        asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))
        return {"status": "ok", "equity": body.equity}

    @renko_router.post("/config")
    async def update_config(body: ConfigInput, symbol: str = "SPY", user=Depends(require_admin)) -> Dict:
        """Update pipeline configuration (resets the pipeline).

        Uses runtime config as the base, then applies the explicit overrides
        from the request body. This ensures admin-adjusted defaults via /config
        API are respected even when per-pipeline overrides are provided.
        """
        # Build overrides from the request body (non-default fields)
        overrides = {
            "brick_size": body.brick_size,
            "brick_size_mode": body.brick_size_mode,
            "reversal_bricks": body.reversal_bricks,
            "bull_trigger_n": body.bull_trigger_n,
            "bear_trigger_n": body.bear_trigger_n,
            "sl_bricks": body.sl_bricks,
            "tp_bricks": body.tp_bricks,
            "trailing_stop": body.trailing_stop,
            "trail_after_bricks": body.trail_after_bricks,
            "trail_distance_bricks": body.trail_distance_bricks,
            "max_trades_per_session": body.max_trades_per_session,
            "max_daily_loss_bricks": body.max_daily_loss_bricks,
            "max_consecutive_losses": body.max_consecutive_losses,
            "cooldown_seconds": body.cooldown_seconds,
            "regime_gate": body.regime_gate,
            "symbol": body.symbol,
        }
        config = await RenkoConfig.from_runtime_config(overrides=overrides)
        pipeline = RenkoPipeline(config)
        key = _pipeline_key(body.symbol)
        _pipelines[key] = pipeline
        return {"status": "ok", "config": config.to_dict()}

    @renko_router.post("/reset")
    @limiter.limit(RATE_LIMIT_WRITE)
    async def reset_pipeline(request: Request, symbol: str = "SPY", user=Depends(require_admin)) -> Dict:
        """Reset the pipeline for a symbol."""
        pipeline = await _get_pipeline_async(symbol)
        pipeline.reset()

        # ── Fire Discord status notification ─────────────────────
        _notify_reset(symbol)

        return {"status": "ok"}

    @renko_router.get("/backtest/stats")
    async def backtest_stats(symbol: str = "SPY", user=Depends(get_authed_user)) -> Dict[str, Any]:
        """Get backtest/trading statistics from the journal."""
        pipeline = await _get_pipeline_async(symbol)
        return _sanitize_numpy(pipeline.journal.get_stats())

    @renko_router.post("/backtest/run")
    async def run_backtest(request: Request, body: BacktestRequest, user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Run a full backtest with Phase 2-7 analytics.

        Uses runtime config as the base, then applies the explicit overrides
        from the request body. This ensures admin-adjusted defaults via /config
        API are respected even when per-backtest overrides are provided.

        Atomic Commit (MCP agent): when the persistent pipeline for this
        symbol is cold (brick_count == 0), the backtest uses the persistent
        pipeline directly so bricks are committed and auto-saved.
        """
        symbol = body.symbol or "SPY"
        user_id = getattr(user, 'sub', None) if user else None
        overrides = {
            "symbol": symbol,
            "brick_size": body.brick_size,
            "sl_bricks": body.sl_bricks,
            "tp_bricks": body.tp_bricks,
            "slippage_bps": body.slippage_bps,
            "commission_bps": body.commission_bps,
            "spread_bps": body.spread_bps,
            "oco_priority": body.oco_priority,
            "market_impact_mode": body.market_impact_mode,
            "adv_shares": body.adv_shares,
            "fill_probability_mode": body.fill_probability_mode,
            "borrow_rate_bps": body.borrow_rate_bps,
            "margin_rate_bps": body.margin_rate_bps,
            "is_hard_to_borrow": body.is_hard_to_borrow,
            "dividend_yield_bps": body.dividend_yield_bps,
            "initial_capital": body.initial_capital,
        }

        # ── Hydrate-on-Cold: use persistent pipeline when it's empty ──
        key = _pipeline_key(symbol, user_id)
        use_persistent = (
            key in _pipelines and _pipelines[key].brick_engine.brick_count == 0
        )

        if use_persistent:
            pipeline = _pipelines[key]
            log.info(
                "backtest/run: using persistent pipeline for %s (cold -> warmup)",
                key,
            )
        else:
            config = await RenkoConfig.from_runtime_config(overrides=overrides)
            pipeline = RenkoPipeline(config)
        result = pipeline.backtest_with_analytics(
            prices=body.prices,
            timestamps=body.timestamps,
            regimes=body.regimes,
            n_trials=body.n_trials,
            confidence_level=body.confidence_level,
        )

        # ── Atomic Commit: persist bricks to Supabase if using persistent pipeline ──
        if use_persistent:
            _pipelines[key] = pipeline
            saved = await _save_pipeline_snapshot_confirmed(symbol, pipeline)
            result["_persistence"] = {
                "saved": saved,
                "store": "L1+L2" if renko_cache.enabled else "L2-only",
                "mode": "cold_warmup",
            }

        return _sanitize_numpy(result)

    @renko_router.post("/backtest/run/stream")
    async def run_backtest_stream(request: Request, body: BacktestStreamRequest, user: TokenData = Depends(require_write)):
        """Run a backtest with SSE streaming (Phase 1).

        Atomic Commit (MCP agent): when the persistent pipeline for this
        symbol is cold (brick_count == 0), the stream uses the persistent
        pipeline directly so bricks are committed to ``_pipelines`` and
        auto-saved to Supabase. When warm, a transient pipeline is used
        for isolated parameter testing without disrupting live state.
        """
        from fastapi.responses import StreamingResponse
        import json

        symbol = body.symbol or "SPY"
        user_id = getattr(user, 'sub', None) if user else None
        overrides = {
            "symbol": symbol,
            "brick_size": body.brick_size,
            "sl_bricks": body.sl_bricks,
            "tp_bricks": body.tp_bricks,
            "slippage_bps": body.slippage_bps,
            "commission_bps": body.commission_bps,
            "spread_bps": body.spread_bps,
            "oco_priority": body.oco_priority,
            "market_impact_mode": body.market_impact_mode,
            "adv_shares": body.adv_shares,
            "fill_probability_mode": body.fill_probability_mode,
            "borrow_rate_bps": body.borrow_rate_bps,
            "margin_rate_bps": body.margin_rate_bps,
        }

        # ── Hydrate-on-Cold: use persistent pipeline when it's empty ──
        # This fixes the "Injection Test" failure where backtest_stream
        # processes 105 ticks but brick_count stays 0 on GET /renko/state.
        key = _pipeline_key(symbol, user_id)
        use_persistent = (
            key in _pipelines and _pipelines[key].brick_engine.brick_count == 0
        )

        if use_persistent:
            # Cold pipeline - warm it up via stream (MCP agent use case)
            pipeline = _pipelines[key]
            log.info(
                "backtest/stream: using persistent pipeline for %s (cold -> warmup)",
                key,
            )
        else:
            # Warm pipeline or new - create transient for isolation
            config = await RenkoConfig.from_runtime_config(overrides=overrides)
            pipeline = RenkoPipeline(config)

        def event_generator():
            for chunk in pipeline.backtest_chunked(
                prices=body.prices,
                timestamps=body.timestamps,
                regimes=body.regimes,
                chunk_size=body.chunk_size,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"

        # If we used the persistent pipeline, commit state after streaming
        if use_persistent:
            # Store back to registry and persist
            _pipelines[key] = pipeline
            # Fire-and-forget save (streaming already returned data)
            asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @renko_router.post("/backtest/optimize")
    async def optimize_backtest(request: Request, body: OptimizationRequest, user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Run parameter sweep optimization with Phase 6 statistical rigor.
        
        Supports min_rr filter: combos where tp_bricks/sl_bricks < min_rr
        are skipped entirely, reducing computation and preventing low R:R
        strategies from contaminating the ranking.
        """
        import numpy as np
        from regime_platform.services.statistics.deflated_sharpe import deflated_sharpe_ratio
        from regime_platform.services.statistics.bootstrap_ci import compute_all_bootstrap_cis
        from regime_platform.services.statistics.multiple_testing import apply_all_corrections

        symbol = body.symbol or "SPY"
        results = []
        skipped_rr = 0

        for brick_size in body.brick_sizes:
            for sl in body.sl_bricks_range:
                for tp in body.tp_bricks_range:
                    # ── R:R filter: skip combos below minimum risk-reward ──
                    if body.min_rr > 0 and (tp / sl) < body.min_rr:
                        skipped_rr += 1
                        continue
                    overrides = {
                        "symbol": symbol,
                        "brick_size": brick_size,
                        "sl_bricks": sl,
                        "tp_bricks": tp,
                        "slippage_bps": body.slippage_bps,
                        "commission_bps": body.commission_bps,
                        "spread_bps": body.spread_bps,
                        "market_impact_mode": body.market_impact_mode,
                        "adv_shares": body.adv_shares,
                        "fill_probability_mode": body.fill_probability_mode,
                        "borrow_rate_bps": body.borrow_rate_bps,
                        "margin_rate_bps": body.margin_rate_bps,
                    }
                    config = await RenkoConfig.from_runtime_config(overrides=overrides)
                    pipeline = RenkoPipeline(config)
                    bt_result = pipeline.backtest_with_analytics(
                        prices=body.prices,
                        timestamps=body.timestamps,
                        regimes=body.regimes,
                        n_trials=1,  # DSR computed on aggregate below
                        confidence_level=body.confidence_level,
                    )
                    stats = bt_result.get("stats", {}).get("journal", bt_result.get("stats", {}))
                    results.append({
                        "brick_size": brick_size,
                        "sl_bricks": sl,
                        "tp_bricks": tp,
                        "total_trades": stats.get("total_trades", 0),
                        "win_rate": stats.get("win_rate", 0),
                        "sharpe": stats.get("sharpe_estimate", 0),
                        "profit_factor": stats.get("profit_factor", 0),
                        "total_pnl_bricks": stats.get("total_pnl_bricks", 0),
                        "max_drawdown_bricks": stats.get("max_drawdown_bricks", 0),
                        "cost_summary": stats.get("cost_summary", {}),
                        "dollar_stats": stats.get("dollar_stats", {}),
                    })

        n_trials = len(results)
        p_values = []
        for r in results:
            # Simple t-test: is Sharpe > 0?
            sharpe = r["sharpe"]
            n_trades = r["total_trades"]
            if n_trades > 2 and sharpe != 0:
                t_stat = sharpe * np.sqrt(n_trades) / np.sqrt(252)
                p_val = 1 - sp_stats.norm.cdf(abs(t_stat))
            else:
                p_val = 1.0
            r["raw_p_value"] = round(p_val, 6)
            p_values.append(p_val)

        # Multiple testing correction (Phase 6)
        if n_trials > 1:
            mtc = apply_all_corrections(p_values, body.multiple_testing_alpha)
            for i, r in enumerate(results):
                r["bonferroni_adjusted_p"] = mtc["corrections"]["bonferroni"]["adjusted_p_values"][i]
                r["holm_adjusted_p"] = mtc["corrections"]["holm_bonferroni"]["adjusted_p_values"][i]
                r["bh_adjusted_p"] = mtc["corrections"]["benjamini_hochberg"]["adjusted_p_values"][i]

            # DSR for the best strategy
            best = max(results, key=lambda x: x["sharpe"])
            if best["sharpe"] > 0 and best["total_trades"] > 2:
                dsr = deflated_sharpe_ratio(
                    observed_sharpe=best["sharpe"],
                    n_trials=n_trials,
                    sample_length=best["total_trades"],
                )
            else:
                dsr = {"dsr": 0.0, "interpretation": "Best Sharpe is non-positive"}
        else:
            mtc = {"summary": {"raw_significant": 0}}
            dsr = {"dsr": 1.0, "interpretation": "Single trial"}

        # Sort by Sharpe descending
        results.sort(key=lambda x: x["sharpe"], reverse=True)

        response = {
            "n_combinations": n_trials,
            "skipped_rr": skipped_rr,
            "results": results,
            "multiple_testing": mtc,
            "deflated_sharpe": dsr,
            "best": results[0] if results else None,
        }

        # Sanitize numpy types (numpy.bool_, numpy.int64, etc.) before Pydantic serialization
        return _sanitize_numpy(response)

    @renko_router.post("/statistics/rigor")
    async def statistical_rigor(request: Request, body: StatisticalRigorRequest, user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Run statistical rigor tests (Phase 6) on trade results."""
        import numpy as np
        from regime_platform.services.statistics.deflated_sharpe import deflated_sharpe_ratio
        from regime_platform.services.statistics.bootstrap_ci import compute_all_bootstrap_cis
        from regime_platform.services.statistics.significance_tests import run_significance_tests

        pnls = np.array(body.trade_pnls)

        result = {}

        # Bootstrap CIs
        if len(pnls) >= 5:
            result["bootstrap_ci"] = compute_all_bootstrap_cis(pnls, body.confidence_level)

        # Deflated Sharpe Ratio
        result["deflated_sharpe"] = deflated_sharpe_ratio(
            observed_sharpe=body.observed_sharpe,
            n_trials=body.n_trials,
            sample_length=len(pnls),
            returns=pnls,
        )

        # Significance tests (if strategy returns provided)
        if body.strategy_returns:
            strat_returns = np.array(body.strategy_returns)
            benchmark = np.array(body.benchmark_returns) if body.benchmark_returns else None
            result["significance_tests"] = run_significance_tests(strat_returns, benchmark)

        return result

    @renko_router.post("/execution/model")
    async def execution_model(request: Request, body: ExecutionModelRequest, user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Compute execution modeling costs (Phase 7) for a trade.

        Uses runtime_config as the source of defaults for any param not
        explicitly provided in the request body. This ensures admin-adjusted
        defaults via the /config API are respected.
        """
        from regime_platform.services.execution.market_impact import compute_market_impact
        from regime_platform.services.execution.fill_probability import compute_fill_probability
        from regime_platform.services.execution.borrow_financing import compute_total_financing_costs
        from regime_platform.services.execution import get_execution_defaults

        # Resolve execution defaults from runtime config
        defaults = await get_execution_defaults()

        result = {}

        # Market impact - use runtime default for sigma_daily and adv if not specified
        sigma_daily = body.sigma_daily if body.sigma_daily != 0.02 else defaults["sigma_daily"]
        adv_shares = body.adv_shares if body.adv_shares != 10_000_000 else defaults["default_adv_shares"]

        result["market_impact"] = compute_market_impact(
            order_shares=body.order_shares,
            adv_shares=adv_shares,
            sigma_daily=sigma_daily,
            avg_price=body.avg_price,
            permanent_impact_coeff=defaults["permanent_impact_coeff"],
            temporary_impact_coeff=defaults["temporary_impact_coeff"],
        )

        # Fill probability - use runtime defaults for time horizon, sensitivity, threshold
        result["fill_probability"] = compute_fill_probability(
            order_type=body.order_type,
            distance_from_mid_pct=body.distance_from_mid_pct,
            volatility_daily=sigma_daily,
            time_horizon_hours=defaults["fill_time_horizon_hours"],
            sensitivity=defaults["fill_sensitivity"],
            threshold=defaults["fill_threshold"],
        )

        # Financing costs - use runtime defaults for rates
        if body.position_notional > 0:
            result["financing_costs"] = compute_total_financing_costs(
                position_notional=body.position_notional,
                holding_days=body.holding_days,
                direction=body.direction,
                is_hard_to_borrow=body.is_hard_to_borrow,
                borrow_rate_bps=body.borrow_rate_bps,
                margin_rate_bps=body.margin_rate_bps,
                dividend_yield_bps=body.dividend_yield_bps,
                hard_to_borrow_premium_bps=defaults["htb_premium_bps"],
            )

        return result

    # ── Sizing Model Endpoints ────────────────────────────────────────

    @renko_router.get("/sizing/model")
    async def get_sizing_model(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get the current position sizing model and parameters for a pipeline.

        Returns the active sizing mode (kelly or masaniello) along with
        all relevant configuration parameters. This is useful for:
        - Checking which sizing model a pipeline is using
        - Reviewing Masaniello batch state (wins, losses, trade index)
        - Validating configuration before backtests
        """
        pipeline = await _get_pipeline_async(symbol)
        config = pipeline.config
        sizing_mode = config.sizing_mode.value

        result: Dict[str, Any] = {
            "symbol": symbol,
            "sizing_mode": sizing_mode,
            "kelly": {
                "kelly_fraction": config.kelly_fraction,
                "max_position_pct": config.max_position_pct,
                "min_position_usd": config.min_position_usd,
                "default_win_rate": config.default_win_rate,
            },
        }

        if sizing_mode == "masaniello":
            result["masaniello"] = {
                "base_risk": config.masaniello_base_risk,
                "min_risk": config.masaniello_min_risk,
                "max_risk": config.masaniello_max_risk,
                "min_prob": config.masaniello_min_prob,
                "min_rr": config.masaniello_min_rr,
                "max_drawdown": config.masaniello_max_drawdown,
                "regime_floor": config.masaniello_regime_floor,
                "use_kelly_overlay": config.masaniello_use_kelly_overlay,
                "batch_size": config.masaniello_batch_size,
                "target_wins": config.masaniello_target_wins,
                "batch_state": {
                    "wins": pipeline.signal_filter._masaniello_batch_wins,
                    "losses": pipeline.signal_filter._masaniello_batch_losses,
                    "trade_index": pipeline.signal_filter._masaniello_trade_index,
                    "initial_equity": pipeline.signal_filter._masaniello_initial_equity,
                    "peak_equity": pipeline.signal_filter._masaniello_peak_equity,
                    "pnl": pipeline.signal_filter._masaniello_pnl,
                },
            }

        return result

    @renko_router.post("/sizing/model")
    async def set_sizing_model(request: Request, body: SizingModelRequest, symbol: str = "SPY", user: TokenData = Depends(require_admin)) -> Dict[str, Any]:
        """Set the position sizing model for a pipeline (admin only).

        Switches between Kelly (simple equity fraction) and Masaniello
        (dynamic adaptive sizing with urgency, quality, drawdown, and
        volatility factors). When switching to Masaniello, the pipeline
        is rebuilt with the new sizing configuration.

        This endpoint is critical for:
        - Backtests: test the same strategy with different sizing models
        - Live trading: switch from Kelly to Masaniello mid-session
        - Risk tuning: adjust Masaniello parameters without full pipeline reset
        """
        from renko.config import SizingMode

        pipeline = await _get_pipeline_async(symbol)
        config = pipeline.config

        # Validate sizing_mode
        mode = body.sizing_mode.lower().strip()
        if mode not in ("kelly", "masaniello"):
            raise HTTPException(status_code=400, detail=f"Invalid sizing_mode '{mode}'. Must be 'kelly' or 'masaniello'.")

        # Build overrides for the new pipeline
        overrides = {
            "sizing_mode": mode,
            "symbol": symbol,
        }

        # Apply Masaniello-specific overrides
        if mode == "masaniello":
            if body.base_risk is not None:
                overrides["masaniello_base_risk"] = body.base_risk
            if body.min_risk is not None:
                overrides["masaniello_min_risk"] = body.min_risk
            if body.max_risk is not None:
                overrides["masaniello_max_risk"] = body.max_risk
            if body.min_prob is not None:
                overrides["masaniello_min_prob"] = body.min_prob
            if body.min_rr is not None:
                overrides["masaniello_min_rr"] = body.min_rr
            if body.max_drawdown is not None:
                overrides["masaniello_max_drawdown"] = body.max_drawdown
            if body.regime_floor is not None:
                overrides["masaniello_regime_floor"] = body.regime_floor
            if body.use_kelly_overlay is not None:
                overrides["masaniello_use_kelly_overlay"] = body.use_kelly_overlay
            if body.batch_size is not None:
                overrides["masaniello_batch_size"] = body.batch_size
            if body.target_wins is not None:
                overrides["masaniello_target_wins"] = body.target_wins

        # Rebuild pipeline with new sizing config
        config = await RenkoConfig.from_runtime_config(overrides=overrides)
        new_pipeline = RenkoPipeline(config)
        key = _pipeline_key(symbol, getattr(user, 'sub', None) if user else None)
        _pipelines[key] = new_pipeline

        log.warning(f"Pipeline {symbol} sizing mode changed to {mode} by user {user.sub}")

        # Auto-save after critical config change (MCP agent safety)
        asyncio.create_task(_save_pipeline_snapshot(symbol, new_pipeline))

        return {
            "status": "ok",
            "symbol": symbol,
            "sizing_mode": mode,
            "config": config.to_dict(),
        }

    @renko_router.get("/live/status")
    async def get_live_status(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get the live execution status for a pipeline."""
        pipeline = await _get_pipeline_async(symbol)
        config = pipeline.config
        return {
            "symbol": symbol,
            "dry_run": config.dry_run,
            "live_enabled": config.live_enabled,
            "execution_mode": pipeline.executor.execution_mode,
            "has_position": pipeline.risk_manager.has_position,
        }

    @renko_router.post("/live/toggle")
    async def toggle_live_execution(request: Request, symbol: str = "SPY", enabled: bool = True, user: TokenData = Depends(require_admin)) -> Dict[str, Any]:
        """
        Toggle live execution for a pipeline (admin only).
        When enabled=False, sets dry_run=True (safe mode).
        When enabled=True, sets dry_run=False and live_enabled=True.
        """
        pipeline = await _get_pipeline_async(symbol)
        pipeline.config.dry_run = not enabled
        pipeline.config.live_enabled = enabled

        # Update executor mode if keys are available
        if enabled:
            pipeline.executor.paper = "paper-api" in pipeline.config.base_url

        mode = "LIVE" if enabled else "DRY_RUN"
        log.warning(f"Pipeline {symbol} execution mode set to {mode} by user {user.sub}")

        # Auto-save after critical config change (MCP agent safety)
        asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))

        # Discord notification
        if DISCORD_AVAILABLE:
            asyncio.create_task(discord_notifier.send_status(
                message=f"Pipeline {symbol} execution mode changed to {mode}",
                symbol=symbol,
                level=AlertLevel.WARNING if enabled else AlertLevel.INFO,
                data={"enabled": enabled, "user": user.sub},
            ))

        return {
            "status": "ok",
            "symbol": symbol,
            "dry_run": pipeline.config.dry_run,
            "live_enabled": pipeline.config.live_enabled,
        }

    @renko_router.post("/snapshot/restore")
    async def restore_snapshot(request: Request, symbol: str = "SPY", user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Restore a pipeline from a Supabase snapshot (if available)."""
        restored = await _restore_pipeline_snapshot(symbol)
        if restored:
            key = _pipeline_key(symbol, getattr(user, 'sub', None) if user else None)
            _pipelines[key] = restored
            return {"status": "ok", "symbol": symbol, "restored": True, "state": restored.get_state().to_dict()}
        return {"status": "ok", "symbol": symbol, "restored": False, "message": "No snapshot available"}

    # ── MCP Autonomy Endpoints ──────────────────────────────────────

    @renko_router.post("/warmup")
    @limiter.limit(RATE_LIMIT_WRITE)
    async def warmup_pipeline(
        request: Request,
        body: WarmupRequest = WarmupRequest(),
        user: TokenData = Depends(require_write),
    ) -> Dict[str, Any]:
        """
        Warm up a Renko pipeline with historical prices - backend-native warmup.

        Supports three modes:
          - ``full``: Always reset and rebuild from scratch (6mo Yahoo data).
            Use when config changed or data is corrupted.
          - ``incremental``: Append only new prices since the last brick.
            Fast (typically <1s). Fails if pipeline is cold (no bricks).
          - ``auto`` (default): Incremental if pipeline is warm (brick_count > 0),
            full if cold. This is the recommended mode.

        Response includes full pipeline state when ``include_state=True`` (default),
        eliminating the need for separate GET /bricks, /classified, /trades, etc.

        Flow (auto mode):
          1. Try Redis L1 → Supabase L2 snapshot restore (instant if fresh <4h)
          2. If pipeline is warm → incremental: fetch recent prices, append
          3. If pipeline is cold → full: fetch 6mo Yahoo, rebuild from scratch
          4. Save snapshot to Redis L1 + Supabase L2
          5. Return full pipeline data (bricks, classified, trades, signals, stats)
        """
        import time as _time
        from datetime import datetime, timezone, timedelta

        symbol = body.symbol
        period = body.period
        mode = body.mode
        include_state = body.include_state
        user_id = getattr(user, 'sub', None) if user else None

        warmup_start = _time.time()

        # ── Step 1: Try snapshot restore (L1 → L2) ──
        # This replaces the old inline Supabase check — _get_pipeline_async
        # already does HydrateOnRequest, but we do an explicit restore here
        # to get the "snapshot_restore" fast path with age info.
        try:
            from regime_platform.core.settings import settings
            if settings.SUPABASE_URL and settings.SUPABASE_KEY:
                import httpx
                url = (
                    f"{settings.SUPABASE_URL}/rest/v1/nt_renko_snapshot"
                    f"?symbol=eq.{symbol}&select=updated_at,stats&order=updated_at.desc&limit=1"
                )
                headers = {
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                }
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        rows = resp.json()
                        if rows and rows[0].get("stats"):
                            updated_at = rows[0].get("updated_at")
                            from regime_platform.core.runtime_config import _parse_timestamp
                            ts = _parse_timestamp(updated_at) if updated_at else 0
                            age_seconds = _time.time() - ts if ts > 0 else float("inf")
                            if age_seconds < 14400:  # 4 hours
                                # Fresh snapshot - restore it directly
                                data = rows[0]["stats"]
                                pipeline = RenkoPipeline.from_snapshot(data)
                                key = _pipeline_key(symbol, user_id)
                                _pipelines[key] = pipeline
                                log.info(
                                    f"Warmup: restored fresh snapshot for {symbol} "
                                    f"(age={age_seconds:.0f}s)"
                                )
                                # Update heartbeat: snapshot restored
                                asyncio.create_task(trading_heartbeat.update_from_pipeline(symbol, pipeline))
                                asyncio.create_task(trading_heartbeat.update(
                                    symbol, source="snapshot_restore", warmup_mode="restore",
                                    snapshot_ts=_time.time(),
                                ))
                                resp_data = {
                                    "status": "ok",
                                    "symbol": symbol,
                                    "source": "snapshot_restore",
                                    "mode": "restore",
                                    "age_seconds": round(age_seconds),
                                    "brick_count": pipeline.brick_engine.brick_count,
                                    "elapsed_ms": round((_time.time() - warmup_start) * 1000),
                                }
                                if include_state:
                                    resp_data.update(_build_full_state(pipeline))
                                return resp_data
        except Exception as e:
            log.debug(f"Warmup snapshot check failed (non-critical): {e}")

        # ── Step 2: Determine warmup mode ──
        # Check if there's an existing warm pipeline in memory
        key = _pipeline_key(symbol, user_id)
        existing_pipeline = _pipelines.get(key)
        existing_brick_count = existing_pipeline.brick_engine.brick_count if existing_pipeline else 0

        effective_mode = mode
        if mode == "auto":
            effective_mode = "incremental" if existing_brick_count > 0 else "full"
        elif mode == "incremental" and existing_brick_count == 0:
            # Can't do incremental on a cold pipeline — fall back to full
            log.info(
                f"Warmup: incremental mode requested but pipeline is cold "
                f"for {symbol} — falling back to full"
            )
            effective_mode = "full"

        log.info(
            f"Warmup: {symbol} mode={effective_mode} "
            f"(requested={mode}, existing_bricks={existing_brick_count})"
        )

        # ── Step 3: Fetch prices ──
        if effective_mode == "incremental":
            # Incremental: only fetch prices after the last brick timestamp
            last_brick = existing_pipeline.brick_engine.last_brick
            if last_brick and last_brick.timestamp > 0:
                # Calculate how many days since last brick
                last_ts = datetime.fromtimestamp(last_brick.timestamp, tz=timezone.utc)
                days_since = (datetime.now(timezone.utc) - last_ts).days
                # Map to Yahoo Finance period strings (valid: 1mo, 3mo, 6mo, 1y...)
                # yfinance with interval="1d" works best with month/year periods
                if days_since <= 30:
                    incremental_period = "1mo"
                elif days_since <= 90:
                    incremental_period = "3mo"
                elif days_since <= 180:
                    incremental_period = "6mo"
                else:
                    incremental_period = "1y"

                try:
                    all_prices, all_timestamps = await _fetch_yahoo_prices(
                        symbol, incremental_period
                    )
                except Exception as e:
                    log.warning(
                        f"Incremental Yahoo fetch failed for {symbol}: {e} "
                        f"— falling back to full warmup"
                    )
                    effective_mode = "full"
                else:
                    # Filter: only keep prices after the last brick's timestamp
                    cutoff_ts = last_brick.timestamp
                    new_prices = []
                    new_timestamps = []
                    for p, t in zip(all_prices, all_timestamps):
                        if t > cutoff_ts:
                            new_prices.append(p)
                            new_timestamps.append(t)

                    if not new_prices:
                        # No new prices — pipeline is already up to date
                        log.info(
                            f"Warmup: no new prices for {symbol} since last brick "
                            f"(cutoff={cutoff_ts})"
                        )
                        # Update heartbeat: still up to date
                        asyncio.create_task(trading_heartbeat.update(
                            symbol, source="up_to_date", warmup_mode="incremental",
                            snapshot_ts=_time.time(),
                        ))
                        resp_data = {
                            "status": "ok",
                            "symbol": symbol,
                            "source": "up_to_date",
                            "mode": "incremental",
                            "brick_count": existing_brick_count,
                            "prices_fed": 0,
                            "new_bricks": 0,
                            "elapsed_ms": round((_time.time() - warmup_start) * 1000),
                        }
                        if include_state:
                            resp_data.update(_build_full_state(existing_pipeline))
                        return resp_data

                    prices, timestamps = new_prices, new_timestamps
                    pipeline = existing_pipeline
                    log.info(
                        f"Incremental warmup: {symbol} has {len(prices)} new prices "
                        f"since last brick (period={incremental_period})"
                    )
            else:
                # No valid last brick timestamp — can't do incremental
                log.warning(
                    f"Warmup: no valid last brick timestamp for {symbol} "
                    f"— falling back to full"
                )
                effective_mode = "full"

        if effective_mode == "full":
            # Full rebuild: fetch Yahoo prices and create fresh pipeline
            try:
                prices, timestamps = await _fetch_yahoo_prices(symbol, period)
            except Exception as e:
                # If Yahoo fetch fails, try restoring a stale snapshot
                log.warning(f"Yahoo fetch failed for {symbol}: {e} — trying stale snapshot")
                restored = await _restore_pipeline_snapshot(symbol, user_id=user_id)
                if restored:
                    _pipelines[key] = restored
                    # Update heartbeat: stale snapshot restored
                    asyncio.create_task(trading_heartbeat.update_from_pipeline(symbol, restored))
                    asyncio.create_task(trading_heartbeat.update(
                        symbol, source="stale_snapshot_restore", warmup_mode="full",
                        snapshot_ts=_time.time(),
                    ))
                    resp_data = {
                        "status": "ok",
                        "symbol": symbol,
                        "source": "stale_snapshot_restore",
                        "mode": "full",
                        "warning": f"Yahoo fetch failed: {e}",
                        "brick_count": restored.brick_engine.brick_count,
                        "elapsed_ms": round((_time.time() - warmup_start) * 1000),
                    }
                    if include_state:
                        resp_data.update(_build_full_state(restored))
                    return resp_data
                raise HTTPException(
                    status_code=502,
                    detail=f"Yahoo Finance fetch failed and no snapshot available: {e}",
                )

            if not prices:
                raise HTTPException(
                    status_code=502,
                    detail=f"Yahoo Finance returned no price data for {symbol}",
                )

            # Create fresh pipeline from runtime config
            config = await RenkoConfig.from_runtime_config(overrides={"symbol": symbol})
            pipeline = RenkoPipeline(config)

        # ── Step 4: Set regime for warmup (enables signals) ──
        if not pipeline._current_regime:
            pipeline.set_regime("low_vol_bull")

        # ── Step 5: Feed prices ──
        brick_count_before = pipeline.brick_engine.brick_count

        if effective_mode == "incremental":
            # Append new ticks WITHOUT resetting the pipeline
            # Use process_tick for each price to preserve existing state
            pipeline.signal_filter.set_warmup_mode(True)
            for i, price in enumerate(prices):
                ts = timestamps[i] if timestamps else float(i)
                pipeline.process_tick(price, ts)
            pipeline.signal_filter.set_warmup_mode(False)
            new_bricks = pipeline.brick_engine.brick_count - brick_count_before
        else:
            # Full rebuild: use backtest (which resets and reprocesses)
            result = pipeline.backtest(
                prices=prices,
                timestamps=timestamps,
                regimes=None,
            )
            new_bricks = pipeline.brick_engine.brick_count

        # ── Step 6: Save snapshot ──
        _pipelines[key] = pipeline
        await _save_pipeline_snapshot(symbol, pipeline, user_id=user_id)

        # ── Step 6.1: Update trading heartbeat (warmup complete) ──
        # The _save_snapshot also updates heartbeat, but we add warmup-specific fields
        asyncio.create_task(trading_heartbeat.update(
            symbol, source="yahoo_fetch", warmup_mode=effective_mode,
        ))

        # ── Step 6.5: Prune stale snapshots (housekeeping) ──
        prune_count = await _prune_stale_snapshots(symbol, max_age_hours=24.0)

        elapsed = _time.time() - warmup_start
        total_bricks = pipeline.brick_engine.brick_count
        journal_stats = pipeline.journal.get_stats()

        # Discord notification
        if DISCORD_AVAILABLE:
            try:
                asyncio.create_task(discord_notifier.send_status(
                    message=f"Pipeline warmup complete: {symbol} — {total_bricks} bricks ({effective_mode})",
                    symbol=symbol,
                    level=AlertLevel.INFO,
                    data={
                        "source": f"backend_warmup_{effective_mode}",
                        "total_bricks": total_bricks,
                        "new_bricks": new_bricks,
                        "total_prices": len(prices),
                        "elapsed_seconds": round(elapsed, 1),
                    },
                ))
            except Exception:
                pass

        log.info(
            f"Warmup complete for {symbol}: {total_bricks} bricks "
            f"(+{new_bricks} new) from {len(prices)} prices "
            f"in {elapsed:.1f}s [{effective_mode}]"
        )

        resp_data = {
            "status": "ok",
            "symbol": symbol,
            "source": "yahoo_fetch",
            "mode": effective_mode,
            "prices_fed": len(prices),
            "brick_count": total_bricks,
            "new_bricks": new_bricks,
            "total_trades": journal_stats.get("total_trades", 0),
            "total_pnl_bricks": journal_stats.get("total_pnl_bricks", 0.0),
            "pruned_snapshots": prune_count,
            "elapsed_ms": round(elapsed * 1000),
        }
        if include_state:
            resp_data.update(_build_full_state(pipeline))
        return resp_data

    @renko_router.post("/snapshot/save")
    async def save_snapshot(
        request: Request,
        symbol: str = "SPY",
        user: TokenData = Depends(require_write),
    ) -> Dict[str, Any]:
        """
        Explicitly save the current pipeline state to Supabase (awaited, confirmed).

        Unlike the automatic periodic saves (fire-and-forget), this endpoint
        awaits the Supabase upsert and returns confirmation. MCP agents should
        use this after critical state changes (regime, equity, position open)
        where persistence must be guaranteed before proceeding.

        The saved snapshot (v3) includes:
          - Full brick history, classified bricks, trade journal
          - Active position with trailing stop state
          - Signal filter state (session counters, Masaniello batch)
          - Pipeline readiness assessment
        """
        pipeline = await _get_pipeline_async(symbol)
        snap = pipeline.snapshot()

        # Awaited, confirmed save - not fire-and-forget
        saved = await _save_pipeline_snapshot_confirmed(symbol, pipeline)

        return {
            "status": "ok" if saved else "failed",
            "symbol": symbol,
            "snapshot_version": snap.get("snapshot_version", 2),
            "brick_count": snap.get("brick_count", 0),
            "total_trades": snap.get("journal_stats", {}).get("total_trades", 0),
            "has_active_position": snap.get("active_position") is not None,
            "snapshot_ts": snap.get("snapshot_ts"),
            "bricks_serialized": len(snap.get("bricks", [])),
            "trades_serialized": len(snap.get("trades", [])),
            "classified_serialized": len(snap.get("classified", [])),
            "readiness": pipeline.readiness,
        }

    # ── EV Computation Endpoints ──────────────────────────────────────

    @renko_router.post("/ev/compute", response_model=EVComputeResponse, summary="Full EV computation with blended P_win")
    async def compute_ev(
        request: Request,
        req: EVComputeRequest,
        user: TokenData = Depends(get_authed_user),
    ) -> EVComputeResponse:
        """
        Compute full Expected Value with 4-source logit-pooled P_win (v5):

        1. **P_pattern**     — historical win-rate for this pattern+regime+direction
                               (None when agent DuckDB is not available server-side)
        2. **P_regime**      — from HMM trend posteriors (auto-detected if not provided)
        3. **P_markov_hold_N** — T^N multi-step hold probability from the Markov
                               transition matrix (built from prices)
        4. **P_timesfm**     — TimesFM next-bar up forecast (optional)

        logit(P_win) = Σ w_i * logit(p_i)  (weights renormalised over available sources)

        Also returns aggression dispatch (passive/mid/aggressive) with order type,
        limit offset, patience bars, and EV-scaled Kelly factor.
        """
        import numpy as np
        
        # 1. Detect regime if trend_probs not provided
        trend_probs = req.trend_probs
        if trend_probs is None and len(req.prices) >= 20:
            try:
                from regime_platform.core.regime_engine import RegimeHMM
                prices = np.array(req.prices, dtype=float)
                model = await RegimeHMM.from_runtime_config()
                model.fit(prices)
                snap = model.predict(prices)
                trend_probs = dict(zip(RegimeHMM.TREND_LABELS, snap.trend_probs))
            except Exception as e:
                log.debug(f"Regime auto-detection failed for EV compute: {e}")
        
        # 2. Build Markov chain from prices
        from regime_platform.services.statistics.markov_chain import MarkovChain
        mc = MarkovChain(brick_size=req.brick_size, window=req.markov_window)
        transition_matrix = mc.fit_from_prices(req.prices) if len(req.prices) >= 3 else None
        
        # 3. Compute blended EV
        from regime_platform.services.statistics.ev_engine import EVEngine
        ev_engine = EVEngine()
        ev_result = ev_engine.compute_ev(
            trend_probs=trend_probs,
            direction=req.direction,
            transition_matrix=transition_matrix,
            current_state=mc.current_state,
            tp_bricks=req.tp_bricks,
            sl_bricks=req.sl_bricks,
            brick_size=req.brick_size,
        )
        
        # 4. Dispatch aggression
        from regime_platform.services.execution.aggression_dispatcher import AggressionDispatcher
        dispatcher = AggressionDispatcher()
        
        # Determine vol_state for patience scaling
        vol_state = "med"  # Default
        if trend_probs:
            # Infer vol_state from regime label if available
            # This is a simplified mapping; in production, use actual vol_probs
            vol_state = "med"
        
        dispatch = dispatcher.dispatch(
            ev_per_dollar=ev_result.ev_per_dollar,
            vol_state=vol_state,
            base_kelly=req.base_kelly,
        )
        
        return EVComputeResponse(
            p_win=ev_result.p_win,
            p_regime=ev_result.p_regime,
            p_pattern=ev_result.p_pattern,
            p_markov=ev_result.p_markov,
            ev=ev_result.ev,
            ev_per_dollar=ev_result.ev_per_dollar,
            rr=ev_result.rr,
            profit=ev_result.profit,
            stake=ev_result.stake,
            allowed=ev_result.allowed,
            aggression=dispatch.aggression,
            ev_scale=dispatch.ev_scale,
            markov_multiplier=ev_result.markov_multiplier,
            order_type=dispatch.order_type,
            patience_bars=dispatch.patience_bars,
            markov_current_state=mc.current_state,
            markov_n_observations=mc.n_observations,
        )

    # ── /ev/sync-journal endpoint REMOVED in v5 ─────────────────────
    # The endpoint called EVEngine.sync_journal_to_stats() which was
    # deleted in v5 (pattern stats live in the agent's DuckDB, not the
    # server). The nt_pattern_stats table is deprecated (see migration
    # 034_deprecate_nt_pattern_stats.sql). Pattern stats now flow from
    # the agent to the EV engine via the orchestrator's agent_payload
    # (p_pattern field), not via a server-side DB write.

else:
    # Stub router for environments without FastAPI
    renko_router = None


# ── Notification helpers (non-blocking, fire-and-forget) ────────────────


def _notify_tick_result(symbol: str, result: Dict[str, Any], pipeline: RenkoPipeline) -> None:
    """
    Fire Discord notifications for signals and trade closes from a tick result.

    This is called after each tick is processed. It inspects the result
    dict for filtered signals and trade close events, then fires the
    appropriate Discord embeds asynchronously (fire-and-forget).

    Never blocks or raises - all notification errors are caught and logged.
    """
    if not DISCORD_AVAILABLE:
        return

    try:
        # 1. Signal detected -> send to #trade-signals
        filtered_data = result.get("filtered")
        if filtered_data and isinstance(filtered_data, dict):
            signal_data = filtered_data.get("original_signal", filtered_data)
            direction = signal_data.get("direction", "UNKNOWN")
            pattern = signal_data.get("pattern_type", signal_data.get("pattern", "unknown"))
            price = signal_data.get("price", 0.0)
            confidence = signal_data.get("strength", signal_data.get("confidence"))
            velocity = signal_data.get("velocity")

            # Get regime and brick_size from pipeline
            regime = pipeline._current_regime
            brick_size = pipeline.config.get_brick_size()

            # Get SL/TP bricks from YAML-backed brick params (v5).
            # The previous implementation hardcoded 3 (sl) and 5 (tp), which
            # drifted from the YAML config after the heavy sweep rewrote
            # brick_params.yaml. Use load_brick_params() so the Discord /
            # Telegram signal embeds match the actual trade SL/TP values.
            from regime_platform.config.brick_params import load_brick_params
            _, sl_bricks, tp_bricks = load_brick_params(symbol)

            # Calculate SL/TP prices for the execution embed
            if direction.upper() in ("LONG", "BUY"):
                sl_price = round(price - sl_bricks * brick_size, 4) if price else None
                tp_price = round(price + tp_bricks * brick_size, 4) if price else None
            else:
                sl_price = round(price + sl_bricks * brick_size, 4) if price else None
                tp_price = round(price - tp_bricks * brick_size, 4) if price else None

            # Update regime shift tracking
            regime_shift = pipeline._update_regime_shift(regime)
            
            # Signal alert -> #trade-signals
            asyncio.create_task(discord_notifier.send_signal(
                symbol=symbol,
                direction=direction,
                pattern=pattern,
                price=price or 0.0,
                confidence=confidence,
                velocity=velocity,
                brick_size=brick_size,
                regime=regime,
                kelly_fraction=pipeline._last_filtered.kelly_fraction if pipeline._last_filtered else None,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_bricks=sl_bricks,
                tp_bricks=tp_bricks,
                prev_regime=pipeline._prev_regime,
                regime_shift=regime_shift,
            ))
            
            # Telegram signal notification
            if TELEGRAM_AVAILABLE:
                telegram_notifier.send_signal(
                    symbol=symbol,
                    direction=direction,
                    pattern=pattern,
                    price=price or 0.0,
                    confidence=confidence,
                    brick_size=brick_size,
                    regime=regime,
                    kelly_fraction=pipeline._last_filtered.kelly_fraction if pipeline._last_filtered else None,
                    masaniello_sizing=(pipeline._last_filtered.sizing_mode == "masaniello") if pipeline._last_filtered else False,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    sl_bricks=sl_bricks,
                    tp_bricks=tp_bricks,
                    prev_regime=pipeline._prev_regime,
                    regime_shift=regime_shift,
                )

            # Execution alert -> #trade-executions
            asyncio.create_task(discord_notifier.send_execution(
                symbol=symbol,
                side="buy" if direction.upper() in ("LONG", "BUY") else "sell",
                qty=1,  # qty managed by executor
                price=price or 0.0,
                sl_price=sl_price,
                tp_price=tp_price,
                status="submitted",
                order_type="bracket",
            ))

        # 2. Trade closed (SL/TP hit) -> send to #trade-executions
        trade_result = result.get("trade_result")
        if trade_result and isinstance(trade_result, dict):
            pnl_bricks = trade_result.get("pnl_bricks", 0.0)
            pnl_dollars = trade_result.get("pnl_dollars", 0.0)
            status = trade_result.get("status", "closed")
            direction = trade_result.get("direction", "")

            asyncio.create_task(discord_notifier.send_execution(
                symbol=symbol,
                side="sell" if direction.upper() in ("LONG", "BUY") else "buy",
                qty=1,
                pnl_bricks=pnl_bricks,
                pnl_dollars=pnl_dollars,
                status=status,
            ))

            # Risk alert for significant losses
            if pnl_bricks <= -5:
                asyncio.create_task(discord_notifier.send_risk_alert(
                    symbol=symbol,
                    risk_type="large_loss",
                    message=f"{symbol} trade closed with significant loss: {pnl_bricks:+.2f} bricks (${pnl_dollars:+.2f})",
                    level=AlertLevel.ERROR,
                    data={"pnl_bricks": pnl_bricks, "pnl_dollars": pnl_dollars},
                ))

    except Exception as e:
        log.warning(f"Discord notification failed (non-critical): {e}")


def _notify_batch_complete(symbol: str, result: Dict[str, Any]) -> None:
    """Fire a status notification when batch processing completes."""
    if not DISCORD_AVAILABLE:
        return

    try:
        total_bricks = result.get("total_bricks", 0)
        total_trades = len(result.get("trades", []))
        total_ticks = result.get("total_ticks", 0)

        # Only notify for significant batches (not tiny test batches)
        if total_ticks < 50:
            return

        asyncio.create_task(discord_notifier.send_status(
            message=f"Batch processing complete: {total_bricks} bricks, {total_trades} trades from {total_ticks} ticks",
            symbol=symbol,
            level=AlertLevel.INFO,
            data={
                "bricks": total_bricks,
                "trades": total_trades,
                "ticks": total_ticks,
            },
        ))
    except Exception as e:
        log.warning(f"Discord batch notification failed (non-critical): {e}")


def _notify_reset(symbol: str) -> None:
    """Fire a status notification when the pipeline is reset."""
    if not DISCORD_AVAILABLE:
        return

    try:
        asyncio.create_task(discord_notifier.send_status(
            message=f"Pipeline reset for {symbol}",
            symbol=symbol,
            level=AlertLevel.WARNING,
        ))
    except Exception as e:
        log.warning(f"Discord reset notification failed (non-critical): {e}")


# ── Pipeline State Persistence ────────────────────────────────────────────


async def _save_pipeline_snapshot_confirmed(symbol: str, pipeline: RenkoPipeline, user_id: Optional[str] = None) -> bool:
    """Save pipeline state snapshot to Redis L1 + Supabase L2 - awaited, returns success boolean.

    v7.3: Delegates to the unified _save_snapshot() with confirmed=True.
    This ensures the same snapshot data is never written to Redis twice
    (the previous fire-and-forget + confirmed dual paths could cause
    duplicate writes when both were called in sequence).
    """
    return await _save_snapshot(symbol, pipeline, user_id=user_id, confirmed=True)


async def _save_pipeline_snapshot(symbol: str, pipeline: RenkoPipeline, user_id: Optional[str] = None) -> None:
    """Save pipeline state snapshot to Redis L1 + Supabase L2 (fire-and-forget).

    v7.3: Delegates to the unified _save_snapshot() with confirmed=False.
    """
    await _save_snapshot(symbol, pipeline, user_id=user_id, confirmed=False)


async def _save_snapshot(
    symbol: str,
    pipeline: RenkoPipeline,
    user_id: Optional[str] = None,
    confirmed: bool = False,
) -> bool:
    """Unified pipeline snapshot save - eliminates duplicate Redis writes.

    v7.3: Consolidates the previous _save_pipeline_snapshot() and
    _save_pipeline_snapshot_confirmed() into a single function with a
    `confirmed` parameter. This prevents the same snapshot from being
    written to Redis twice when both code paths execute in sequence.

    When confirmed=False (fire-and-forget):
      - Redis L1 write is a background task
      - Supabase L2 write is a background task
      - Returns True always (caller ignores result)

    When confirmed=True (awaited):
      - Redis L1 write is a background task (still fast, non-blocking)
      - Supabase L2 write is awaited
      - Returns True/False based on Supabase write success
    """
    try:
        snap = pipeline.snapshot()
        brick_size = snap["config"].get("brick_size", 0.50)

        # ── v8.0 REMOVED: per-snapshot heartbeat + config writes ──
        # trading:heartbeat:{symbol} is now liveness-only (written by the
        # SweepOrchestrator on LightningAI every sweep cycle). trading:config:{symbol}
        # is the Golden State, written exclusively by the SweepOrchestrator.
        #
        # asyncio.create_task(trading_heartbeat.update_from_pipeline(symbol, pipeline))
        # asyncio.create_task(trading_heartbeat.update(symbol, snapshot_ts=time.time()))

        # ── Dual-write: Redis L1 (fast, always non-blocking) ──
        if renko_cache.enabled:
            asyncio.create_task(renko_cache.save(symbol, brick_size, snap))

        # ── Dual-write: Supabase L2 (durable) ──
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return False

        import httpx

        url = f"{settings.SUPABASE_URL}/rest/v1/nt_renko_snapshot"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        journal_stats = snap.get("journal_stats", {})
        row = {
            "symbol": symbol,
            "brick_size": brick_size,
            "total_bricks": snap.get("brick_count", 0),
            "total_trades": journal_stats.get("total_trades", 0),
            "total_pnl_bricks": journal_stats.get("total_pnl_bricks", 0.0),
            "config": snap.get("config", {}),
            "stats": snap,
            "prices_fed": 0,
            "period": "pipeline-persist",
        }
        # Session Key Stability: include user_id for multi-user isolation
        if user_id:
            row["user_id"] = user_id

        if confirmed:
            # Awaited write - return success/failure
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=row)
                if resp.status_code in (200, 201, 204):
                    n_bricks = len(snap.get("bricks", []))
                    n_trades = len(snap.get("trades", []))
                    has_pos = snap.get("active_position") is not None
                    l1_status = "L1+L2" if renko_cache.enabled else "L2-only"
                    log.info(
                        f"Confirmed snapshot save ({l1_status}) for {symbol}: "
                        f"{n_bricks} bricks, {n_trades} trades, "
                        f"active_position={has_pos}, user_id={user_id}"
                    )
                    return True
                else:
                    log.warning(
                        f"Snapshot save FAILED for {symbol}: "
                        f"HTTP {resp.status_code} - {resp.text[:200]}"
                    )
                    return False
        else:
            # Fire-and-forget write - don't block the caller
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=row)
                if resp.status_code in (200, 201, 204):
                    n_bricks = len(snap.get("bricks", []))
                    n_trades = len(snap.get("trades", []))
                    log.debug(
                        f"Saved pipeline snapshot for {symbol}: "
                        f"{n_bricks} bricks, {n_trades} trades"
                    )
                else:
                    log.debug(f"Pipeline snapshot save failed for {symbol}: {resp.status_code}")
            return True
    except Exception as e:
        if confirmed:
            log.warning(f"Snapshot save error for {symbol}: {e}")
        else:
            log.debug(f"Pipeline snapshot save error for {symbol}: {e}")
        return False


async def _restore_pipeline_snapshot(symbol: str, user_id: Optional[str] = None) -> Optional[RenkoPipeline]:
    """Restore a pipeline from the best available cache tier. Returns None on failure.

    Tiered restore (MCP agent perspective):
      L1: Redis    - sub-5ms, compressed JSON, 4h TTL
      L2: Supabase - ~100ms, durable, full history

    v2+ snapshots include ``bricks``, ``classified``, and ``trades`` arrays
    inside the ``stats`` JSONB column, allowing full pipeline hydration
    without a warmup cycle. v3 adds ``active_position`` and ``signal_filter_state``.

    Session Key Stability: when user_id is provided, queries Supabase with
    user_id filter for user-scoped restore. Falls back to admin (user_id=NULL)
    namespace if no user-scoped snapshot exists.
    """
    # ── Tier 1: Redis L1 cache (<5ms) ──
    if renko_cache.enabled:
        try:
            data = await renko_cache.load(symbol)
            if data:
                pipeline = RenkoPipeline.from_snapshot(data)
                n_bricks = len(data.get("bricks", []))
                n_trades = len(data.get("trades", []))
                log.info(
                    f"Restored pipeline from Redis L1 for {symbol}: "
                    f"{n_bricks} bricks, {n_trades} trades"
                )
                return pipeline
        except Exception as e:
            log.debug(f"Redis L1 restore failed for {symbol}: {e} - trying Supabase L2")

    # ── Tier 2: Supabase L2 (~100ms) ──
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return None

        import httpx

        url = f"{settings.SUPABASE_URL}/rest/v1/nt_renko_snapshot"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }

        # Session Key Stability: try user-scoped snapshot first, then admin namespace
        params_list = []
        if user_id:
            # User-scoped lookup (user_id = specific user)
            params_list.append({
                "symbol": f"eq.{symbol}",
                "user_id": f"eq.{user_id}",
                "select": "stats",
                "order": "updated_at.desc",
                "limit": "1",
            })
        # Admin namespace fallback (user_id = null)
        params_list.append({
            "symbol": f"eq.{symbol}",
            "select": "stats",
            "order": "updated_at.desc",
            "limit": "1",
        })

        async with httpx.AsyncClient(timeout=10.0) as client:
            for params in params_list:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows and rows[0].get("stats"):
                        data = rows[0]["stats"]
                        if isinstance(data, str):
                            import json
                            data = json.loads(data)
                        # Skip empty snapshots (brick_count=0)
                        if data.get("brick_count", 0) == 0 and not data.get("bricks"):
                            log.debug(
                                f"Skipping empty snapshot for {symbol} "
                                f"(user_id={params.get('user_id', 'null')})"
                            )
                            continue
                        pipeline = RenkoPipeline.from_snapshot(data)
                        n_bricks = len(data.get("bricks", []))
                        n_trades = len(data.get("trades", []))
                        n_classified = len(data.get("classified", []))
                        log.info(
                            f"Restored pipeline from Supabase L2 for {symbol}: "
                            f"{n_bricks} bricks, {n_classified} classified, {n_trades} trades, "
                            f"regime={data.get('regime')}, equity={data.get('equity')}"
                        )
                        # Backfill Redis L1 so next restore is faster
                        brick_size = data.get("config", {}).get("brick_size", 0.50)
                        asyncio.create_task(renko_cache.save(symbol, brick_size, data))
                        return pipeline
    except Exception as e:
        log.debug(f"Pipeline snapshot restore error for {symbol}: {e}")

    return None


async def _fetch_yahoo_prices(symbol: str, period: str = "6mo") -> tuple:
    """
    Fetch historical closing prices from Yahoo Finance.

    Uses the ``yfinance`` library (pure Python, no external deps).
    Falls back to an HTTP scrape of Yahoo's chart API if yfinance is
    not installed.

    Returns
    -------
    (prices, timestamps) : tuple[list[float], list[float]]
        Parallel arrays of closing prices and Unix timestamps.
    """
    # ── Primary: yfinance ──
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval="1d")
        if hist.empty:
            raise ValueError(f"No data returned for {symbol}")
        prices = hist["Close"].dropna().tolist()
        timestamps = hist.index.astype("int64") // 10**9  # nanos -> epoch seconds
        timestamps = timestamps.tolist()
        log.info(f"yfinance: fetched {len(prices)} prices for {symbol} (period={period})")
        return prices, timestamps
    except ImportError:
        log.debug("yfinance not installed - falling back to Yahoo chart API")
    except Exception as e:
        log.warning(f"yfinance failed for {symbol}: {e} - trying Yahoo chart API")

    # ── Fallback: Yahoo Finance v8 chart API (no deps) ──
    import httpx
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={period}&interval=1d"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise RuntimeError(f"Yahoo chart API returned {resp.status_code}")
        data = resp.json()

    result = data.get("chart", {}).get("result", [])
    if not result:
        raise RuntimeError(f"Yahoo chart API returned no results for {symbol}")

    timestamps = result[0].get("timestamp", [])
    quotes = result[0].get("indicators", {}).get("quote", [{}])
    closes = quotes[0].get("close", []) if quotes else []

    # Filter out None values (market holidays, missing data)
    prices = []
    clean_ts = []
    for ts, price in zip(timestamps, closes):
        if price is not None and ts is not None:
            prices.append(float(price))
            clean_ts.append(float(ts))

    if not prices:
        raise RuntimeError(f"No valid closing prices for {symbol}")

    log.info(f"Yahoo chart API: fetched {len(prices)} prices for {symbol}")
    return prices, clean_ts


async def _prune_stale_snapshots(symbol: str, max_age_hours: float = 24.0) -> int:
    """Delete stale Supabase snapshot rows older than max_age_hours.

    Prevents unbounded table growth by pruning rows that are too old to be
    useful for restore (superseded by newer snapshots or warmup runs).

    Returns the number of rows deleted, or 0 if pruning was skipped.
    This is a housekeeping operation - failures are logged but never raised.

    MCP agent perspective: stale data wastes L2 roundtrips. By pruning on
    warmup, we ensure only useful snapshots exist, keeping restore fast.
    """
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return 0

        import httpx
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()

        url = f"{settings.SUPABASE_URL}/rest/v1/nt_renko_snapshot"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        params = {
            "symbol": f"eq.{symbol}",
            "select": "id,updated_at",
            "updated_at": f"lt.{cutoff_iso}",
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            # First, count stale rows
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return 0

            stale_rows = resp.json()
            if not stale_rows:
                return 0

            stale_ids = [str(r["id"]) for r in stale_rows]
            n_stale = len(stale_rows)

            # Delete stale rows
            delete_params = {"id": f"in.({','.join(stale_ids)})"}
            resp = await client.delete(url, headers=headers, params=delete_params)
            if resp.status_code in (200, 204):
                log.info(
                    f"Pruned {n_stale} stale snapshot(s) for {symbol} "
                    f"(older than {max_age_hours}h)"
                )
                return n_stale
            else:
                log.debug(
                    f"Snapshot prune failed for {symbol}: HTTP {resp.status_code}"
                )
                return 0

    except Exception as e:
        log.debug(f"Snapshot prune error for {symbol}: {e}")
        return 0


# ── Execution Bridge (async - called from router) ────────────────────


async def _execute_signal(pending: dict, user: TokenData, symbol: str) -> Optional[dict]:
    """
    Execute a filtered signal as an Alpaca order.
    Resolves user credentials, creates an executor, and submits the order.
    Returns the OrderResult as a dict, or None on failure.
    """
    try:
        from regime_platform.services.alpaca_stream import resolve_alpaca_credentials
        from renko.executor import AlpacaExecutor

        # Resolve user's Alpaca credentials
        creds = await resolve_alpaca_credentials(user.sub, org_id=user.org_id)
        if not creds:
            log.warning(f"No Alpaca credentials for user {user.sub} - order not submitted")
            return {"status": "skipped", "error": "No Alpaca credentials configured"}

        api_key, secret_key, base_url = creds
        is_paper = "paper-api" in base_url

        # Create a per-request executor with user's credentials
        executor = AlpacaExecutor(
            api_key=api_key,
            api_secret=secret_key,
            base_url=base_url,
            paper=is_paper,
            use_bracket_orders=True,
        )

        filtered = pending["filtered"]
        brick_size = pending["brick_size"]
        sl_bricks = pending["sl_bricks"]
        tp_bricks = pending["tp_bricks"]

        # Execute the order
        order_result = await executor.execute(
            filtered=filtered,
            symbol=symbol,
            brick_size=brick_size,
            sl_bricks=sl_bricks,
            tp_bricks=tp_bricks,
        )

        log.info(f"Order submitted: {order_result.order_id} status={order_result.status.value} mode={executor.execution_mode}")

        # Fire Discord execution notification
        if DISCORD_AVAILABLE and order_result.is_success:
            signal = filtered.original_signal
            direction = signal.direction.value
            side = "buy" if direction.upper() in ("LONG", "BUY") else "sell"
            asyncio.create_task(discord_notifier.send_execution(
                symbol=symbol,
                side=side,
                qty=order_result.filled_qty or 0,
                price=order_result.filled_price or signal.price,
                order_id=order_result.order_id,
                status=order_result.status.value,
                order_type="bracket",
            ))

        return order_result.to_dict()

    except Exception as e:
        log.error(f"Executor bridge failed: {e}")
        return {"status": "error", "error": str(e)}


async def _close_pipeline_position(pending: dict, user: TokenData) -> Optional[dict]:
    """
    Close an Alpaca position when the pipeline's risk manager closes.
    """
    try:
        from regime_platform.services.alpaca_stream import resolve_alpaca_credentials
        from renko.executor import AlpacaExecutor

        creds = await resolve_alpaca_credentials(user.sub, org_id=user.org_id)
        if not creds:
            log.warning(f"No Alpaca credentials for user {user.sub} - position close not submitted")
            return {"status": "skipped", "error": "No Alpaca credentials configured"}

        api_key, secret_key, base_url = creds
        is_paper = "paper-api" in base_url
        symbol = pending["symbol"]

        executor = AlpacaExecutor(
            api_key=api_key,
            api_secret=secret_key,
            base_url=base_url,
            paper=is_paper,
        )

        close_result = await executor.close_position(symbol)
        log.info(f"Position close submitted: {close_result.order_id} status={close_result.status.value}")

        # Fire Discord close notification
        if DISCORD_AVAILABLE:
            trade_result = pending.get("trade_result")
            pnl_bricks = trade_result.pnl_bricks if trade_result else 0
            pnl_dollars = trade_result.pnl_dollars if trade_result else 0
            direction = trade_result.direction.value if trade_result else ""
            asyncio.create_task(discord_notifier.send_execution(
                symbol=symbol,
                side="sell" if direction.upper() in ("LONG", "BUY") else "buy",
                qty=1,
                pnl_bricks=pnl_bricks,
                pnl_dollars=pnl_dollars,
                status=f"closed_{trade_result.status.value}" if trade_result else "closed",
                order_id=close_result.order_id,
            ))

        return close_result.to_dict()

    except Exception as e:
        log.error(f"Executor close bridge failed: {e}")
        return {"status": "error", "error": str(e)}
