"""
FastAPI Router — exposes the Renko HFT pipeline as REST endpoints.

These endpoints can be mounted on the existing FastAPI backend
running on Render, or used standalone.

Mount with::

    from renko.router import renko_router
    app.include_router(renko_router, prefix="/renko", tags=["renko"])

Authentication:
  All endpoints require authentication via the unified auth dependency
  (Clerk JWT → local JWT → API key → dev bypass). Auth levels:
    - get_authed_user: Read-only endpoints — state, stats, bricks, etc. (any role)
    - require_write: Mutations — tick ingestion, backtests, regime, equity (admin/trader)
    - require_admin: Destructive — /config, /reset (admin only)

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

# FastAPI is optional — the module works without it for backtesting
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

# Discord notifications (graceful import — not required for pipeline to work)
try:
    from notifications.discord import discord_notifier, AlertLevel
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

log = logging.getLogger(__name__)


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


# ── Pipeline registry ─────────────────────────────────────────────

# In-memory registry of active pipelines (one per symbol)
_pipelines: Dict[str, RenkoPipeline] = {}


def _get_pipeline(symbol: str = "SPY", config: Optional[RenkoConfig] = None) -> RenkoPipeline:
    """Get or create a pipeline for a symbol."""
    if symbol not in _pipelines:
        if config is None:
            config = RenkoConfig(symbol=symbol)
        _pipelines[symbol] = RenkoPipeline(config)
    return _pipelines[symbol]


async def _get_pipeline_with_restore(symbol: str = "SPY") -> RenkoPipeline:
    """Get or create a pipeline for a symbol, restoring from Supabase snapshot if available."""
    if symbol not in _pipelines:
        # Try to restore from Supabase snapshot
        restored = await _restore_pipeline_snapshot(symbol)
        if restored:
            _pipelines[symbol] = restored
        else:
            _pipelines[symbol] = RenkoPipeline(RenkoConfig(symbol=symbol))
    return _pipelines[symbol]


# ── Router ─────────────────────────────────────────────────────────

if FASTAPI_AVAILABLE:
    renko_router = APIRouter(prefix="/renko", tags=["renko"])

    @limiter.limit(RATE_LIMIT_WRITE)
    @renko_router.post("/tick")
    async def process_tick(request: Request, body: TickInput, user: TokenData = Depends(require_write)) -> Dict[str, Any]:
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

        # ── Save pipeline snapshot periodically (every 100th tick) ───
        if pipeline.brick_engine.brick_count % 100 == 0:
            asyncio.create_task(_save_pipeline_snapshot(symbol, pipeline))

        return result

    @limiter.limit(RATE_LIMIT_WRITE)
    @renko_router.post("/tick/batch")
    async def process_batch(request: Request, body: BatchTickInput, symbol: str = "SPY", user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Process a batch of ticks (backtest / warm-up). Enables warmup mode to bypass session/cooldown filters."""
        # Body symbol takes precedence over query param
        active_symbol = body.symbol or symbol
        pipeline = await _get_pipeline_with_restore(active_symbol)
        result = pipeline.backtest(
            prices=body.prices,
            timestamps=body.timestamps,
            regimes=body.regimes,
        )

        # ── Fire Discord status notification for batch completion ─
        _notify_batch_complete(active_symbol, result)

        # ── Save pipeline snapshot after batch processing ─────────────
        asyncio.create_task(_save_pipeline_snapshot(active_symbol, pipeline))

        return result

    @renko_router.get("/state")
    async def get_state(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get current pipeline state."""
        pipeline = _get_pipeline(symbol)
        return pipeline.get_state().to_dict()

    @renko_router.get("/stats")
    async def get_stats(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get comprehensive pipeline statistics."""
        pipeline = _get_pipeline(symbol)
        return pipeline.get_stats()

    @renko_router.get("/bricks")
    async def get_bricks(symbol: str = "SPY", last_n: int = 100, user: TokenData = Depends(get_authed_user)) -> List[Dict]:
        """Get recent bricks."""
        pipeline = _get_pipeline(symbol)
        bricks = list(pipeline.brick_engine.history)
        return [b.to_dict() for b in bricks[-last_n:]]

    @renko_router.get("/classified")
    async def get_classified(symbol: str = "SPY", last_n: int = 100, user: TokenData = Depends(get_authed_user)) -> List[Dict]:
        """Get recent classified bricks."""
        pipeline = _get_pipeline(symbol)
        classified = list(pipeline.swing_classifier.classified_history)
        return [c.to_dict() for c in classified[-last_n:]]

    @renko_router.get("/signals")
    async def get_signals(symbol: str = "SPY", last_n: int = 50, user: TokenData = Depends(get_authed_user)) -> List[Dict]:
        """Get recent pattern signals."""
        pipeline = _get_pipeline(symbol)
        signals = pipeline.pattern_detector.signals
        return [s.to_dict() for s in signals[-last_n:]]

    @renko_router.get("/trades")
    async def get_trades(symbol: str = "SPY", last_n: int = 50, user: TokenData = Depends(get_authed_user)) -> List[Dict]:
        """Get recent trade records."""
        pipeline = _get_pipeline(symbol)
        trades = list(pipeline.journal.trades)
        return [t.to_dict() for t in trades[-last_n:]]

    @renko_router.get("/swing-points")
    async def get_swing_points(symbol: str = "SPY", last_n: int = 50, user: TokenData = Depends(get_authed_user)) -> List[Dict]:
        """Get detected swing points."""
        pipeline = _get_pipeline(symbol)
        points = pipeline.swing_classifier.swing_points
        return [p.to_dict() for p in points[-last_n:]]

    @renko_router.post("/regime")
    async def set_regime(body: RegimeInput, symbol: str = "SPY", user: TokenData = Depends(require_write)) -> Dict:
        """Update the current HMM regime."""
        pipeline = _get_pipeline(symbol)
        pipeline.set_regime(body.regime)
        return {"status": "ok", "regime": body.regime}

    @renko_router.post("/equity")
    async def set_equity(body: EquityInput, symbol: str = "SPY", user: TokenData = Depends(require_write)) -> Dict:
        """Update the current account equity."""
        pipeline = _get_pipeline(symbol)
        pipeline.set_equity(body.equity)
        return {"status": "ok", "equity": body.equity}

    @renko_router.post("/config")
    async def update_config(body: ConfigInput, symbol: str = "SPY", user: TokenData = Depends(require_admin)) -> Dict:
        """Update pipeline configuration (resets the pipeline)."""
        config = RenkoConfig(
            brick_size=body.brick_size,
            brick_size_mode=body.brick_size_mode,
            reversal_bricks=body.reversal_bricks,
            bull_trigger_n=body.bull_trigger_n,
            bear_trigger_n=body.bear_trigger_n,
            sl_bricks=body.sl_bricks,
            tp_bricks=body.tp_bricks,
            trailing_stop=body.trailing_stop,
            trail_after_bricks=body.trail_after_bricks,
            trail_distance_bricks=body.trail_distance_bricks,
            max_trades_per_session=body.max_trades_per_session,
            max_daily_loss_bricks=body.max_daily_loss_bricks,
            max_consecutive_losses=body.max_consecutive_losses,
            cooldown_seconds=body.cooldown_seconds,
            regime_gate=body.regime_gate,
            symbol=body.symbol,
        )
        pipeline = RenkoPipeline(config)
        _pipelines[symbol] = pipeline
        return {"status": "ok", "config": config.to_dict()}

    @limiter.limit(RATE_LIMIT_WRITE)
    @renko_router.post("/reset")
    async def reset_pipeline(request: Request, symbol: str = "SPY", user: TokenData = Depends(require_admin)) -> Dict:
        """Reset the pipeline for a symbol."""
        pipeline = _get_pipeline(symbol)
        pipeline.reset()

        # ── Fire Discord status notification ─────────────────────
        _notify_reset(symbol)

        return {"status": "ok"}

    @renko_router.get("/backtest/stats")
    async def backtest_stats(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get backtest/trading statistics from the journal."""
        pipeline = _get_pipeline(symbol)
        return pipeline.journal.get_stats()

    @renko_router.post("/backtest/run")
    async def run_backtest(request: Request, body: BacktestRequest, user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Run a full backtest with Phase 2-7 analytics."""
        symbol = body.symbol or "SPY"
        config = RenkoConfig(
            symbol=symbol,
            brick_size=body.brick_size,
            sl_bricks=body.sl_bricks,
            tp_bricks=body.tp_bricks,
            slippage_bps=body.slippage_bps,
            commission_bps=body.commission_bps,
            spread_bps=body.spread_bps,
            oco_priority=body.oco_priority,
            market_impact_mode=body.market_impact_mode,
            adv_shares=body.adv_shares,
            fill_probability_mode=body.fill_probability_mode,
            borrow_rate_bps=body.borrow_rate_bps,
            margin_rate_bps=body.margin_rate_bps,
            is_hard_to_borrow=body.is_hard_to_borrow,
            dividend_yield_bps=body.dividend_yield_bps,
            initial_capital=body.initial_capital,
        )
        pipeline = RenkoPipeline(config)
        result = pipeline.backtest_with_analytics(
            prices=body.prices,
            timestamps=body.timestamps,
            regimes=body.regimes,
            n_trials=body.n_trials,
            confidence_level=body.confidence_level,
        )
        return result

    @renko_router.post("/backtest/run/stream")
    async def run_backtest_stream(request: Request, body: BacktestStreamRequest, user: TokenData = Depends(require_write)):
        """Run a backtest with SSE streaming (Phase 1)."""
        from fastapi.responses import StreamingResponse
        import json

        symbol = body.symbol or "SPY"
        config = RenkoConfig(
            symbol=symbol,
            brick_size=body.brick_size,
            sl_bricks=body.sl_bricks,
            tp_bricks=body.tp_bricks,
            slippage_bps=body.slippage_bps,
            commission_bps=body.commission_bps,
            spread_bps=body.spread_bps,
            oco_priority=body.oco_priority,
            market_impact_mode=body.market_impact_mode,
            adv_shares=body.adv_shares,
            fill_probability_mode=body.fill_probability_mode,
            borrow_rate_bps=body.borrow_rate_bps,
            margin_rate_bps=body.margin_rate_bps,
        )
        pipeline = RenkoPipeline(config)

        def event_generator():
            for chunk in pipeline.backtest_chunked(
                prices=body.prices,
                timestamps=body.timestamps,
                regimes=body.regimes,
                chunk_size=body.chunk_size,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @renko_router.post("/backtest/optimize")
    async def optimize_backtest(request: Request, body: OptimizationRequest, user: TokenData = Depends(require_write)) -> Dict[str, Any]:
        """Run parameter sweep optimization with Phase 6 statistical rigor."""
        import numpy as np
        from regime_platform.services.statistics.deflated_sharpe import deflated_sharpe_ratio
        from regime_platform.services.statistics.bootstrap_ci import compute_all_bootstrap_cis
        from regime_platform.services.statistics.multiple_testing import apply_all_corrections

        symbol = body.symbol or "SPY"
        results = []

        for brick_size in body.brick_sizes:
            for sl in body.sl_bricks_range:
                for tp in body.tp_bricks_range:
                    config = RenkoConfig(
                        symbol=symbol,
                        brick_size=brick_size,
                        sl_bricks=sl,
                        tp_bricks=tp,
                        slippage_bps=body.slippage_bps,
                        commission_bps=body.commission_bps,
                        spread_bps=body.spread_bps,
                        market_impact_mode=body.market_impact_mode,
                        adv_shares=body.adv_shares,
                        fill_probability_mode=body.fill_probability_mode,
                        borrow_rate_bps=body.borrow_rate_bps,
                        margin_rate_bps=body.margin_rate_bps,
                    )
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

        return {
            "n_combinations": n_trials,
            "results": results,
            "multiple_testing": mtc,
            "deflated_sharpe": dsr,
            "best": results[0] if results else None,
        }

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
        """Compute execution modeling costs (Phase 7) for a trade."""
        from regime_platform.services.execution.market_impact import compute_market_impact
        from regime_platform.services.execution.fill_probability import compute_fill_probability
        from regime_platform.services.execution.borrow_financing import compute_total_financing_costs

        result = {}

        # Market impact
        result["market_impact"] = compute_market_impact(
            order_shares=body.order_shares,
            adv_shares=body.adv_shares,
            sigma_daily=body.sigma_daily,
            avg_price=body.avg_price,
        )

        # Fill probability
        result["fill_probability"] = compute_fill_probability(
            order_type=body.order_type,
            distance_from_mid_pct=body.distance_from_mid_pct,
            volatility_daily=body.sigma_daily,
        )

        # Financing costs
        if body.position_notional > 0:
            result["financing_costs"] = compute_total_financing_costs(
                position_notional=body.position_notional,
                holding_days=body.holding_days,
                direction=body.direction,
                is_hard_to_borrow=body.is_hard_to_borrow,
                borrow_rate_bps=body.borrow_rate_bps,
                margin_rate_bps=body.margin_rate_bps,
                dividend_yield_bps=body.dividend_yield_bps,
            )

        return result

    @renko_router.get("/live/status")
    async def get_live_status(symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Get the live execution status for a pipeline."""
        pipeline = _get_pipeline(symbol)
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
        pipeline = _get_pipeline(symbol)
        pipeline.config.dry_run = not enabled
        pipeline.config.live_enabled = enabled

        # Update executor mode if keys are available
        if enabled:
            pipeline.executor.paper = "paper-api" in pipeline.config.base_url

        mode = "LIVE" if enabled else "DRY_RUN"
        log.warning(f"Pipeline {symbol} execution mode set to {mode} by user {user.sub}")

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
            _pipelines[symbol] = restored
            return {"status": "ok", "symbol": symbol, "restored": True, "state": restored.get_state().to_dict()}
        return {"status": "ok", "symbol": symbol, "restored": False, "message": "No snapshot available"}

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

    Never blocks or raises — all notification errors are caught and logged.
    """
    if not DISCORD_AVAILABLE:
        return

    try:
        # 1. Signal detected → send to #trade-signals
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

            # Calculate SL/TP prices for the execution embed
            if direction.upper() in ("LONG", "BUY"):
                sl_price = round(price - 3 * brick_size, 4) if price else None
                tp_price = round(price + 5 * brick_size, 4) if price else None
            else:
                sl_price = round(price + 3 * brick_size, 4) if price else None
                tp_price = round(price - 5 * brick_size, 4) if price else None

            # Signal alert → #trade-signals
            asyncio.create_task(discord_notifier.send_signal(
                symbol=symbol,
                direction=direction,
                pattern=pattern,
                price=price or 0.0,
                confidence=confidence,
                velocity=velocity,
                brick_size=brick_size,
                regime=regime,
            ))

            # Execution alert → #trade-executions
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

        # 2. Trade closed (SL/TP hit) → send to #trade-executions
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


async def _save_pipeline_snapshot(symbol: str, pipeline: RenkoPipeline) -> None:
    """Save pipeline state snapshot to Supabase (fire-and-forget, non-critical)."""
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return

        import httpx
        import json

        snap = pipeline.snapshot()
        url = f"{settings.SUPABASE_URL}/rest/v1/ta_renko_snapshot"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        journal_stats = snap.get("journal_stats", {})
        row = {
            "symbol": symbol,
            "brick_size": snap["config"].get("brick_size", 0.50),
            "total_bricks": snap.get("brick_count", 0),
            "total_trades": journal_stats.get("total_trades", 0),
            "total_pnl_bricks": journal_stats.get("total_pnl_bricks", 0.0),
            "config": snap.get("config", {}),
            "stats": snap,  # full snapshot stored in stats JSONB for restore
            "prices_fed": 0,
            "period": "pipeline-persist",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=row)
            if resp.status_code in (200, 201, 204):
                log.debug(f"Saved pipeline snapshot for {symbol}")
            else:
                log.debug(f"Pipeline snapshot save failed for {symbol}: {resp.status_code}")
    except Exception as e:
        log.debug(f"Pipeline snapshot save error for {symbol}: {e}")


async def _restore_pipeline_snapshot(symbol: str) -> Optional[RenkoPipeline]:
    """Restore a pipeline from a Supabase snapshot (if available). Returns None on failure."""
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return None

        import httpx

        url = f"{settings.SUPABASE_URL}/rest/v1/ta_renko_snapshot"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        params = {
            "symbol": f"eq.{symbol}",
            "select": "stats",
            "order": "updated_at.desc",
            "limit": "1",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                rows = resp.json()
                if rows and rows[0].get("stats"):
                    data = rows[0]["stats"]
                    if isinstance(data, str):
                        import json
                        data = json.loads(data)
                    pipeline = RenkoPipeline.from_snapshot(data)
                    log.info(f"Restored pipeline snapshot for {symbol} (regime={data.get('regime')}, equity={data.get('equity')})")
                    return pipeline
    except Exception as e:
        log.debug(f"Pipeline snapshot restore error for {symbol}: {e}")

    return None


# ── Execution Bridge (async — called from router) ────────────────────


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
            log.warning(f"No Alpaca credentials for user {user.sub} — order not submitted")
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
            log.warning(f"No Alpaca credentials for user {user.sub} — position close not submitted")
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
