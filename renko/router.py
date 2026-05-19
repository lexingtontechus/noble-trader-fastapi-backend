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
    - get_authed_user: Read endpoints + tick ingestion (any role)
    - require_write: State mutations — /regime, /equity (admin/trader)
    - require_admin: Destructive — /config, /reset (admin only)

Backtest endpoints (isolated — never affect live pipeline):
  POST /renko/backtest/run      — Run a full Renko backtest on historical prices
  POST /renko/backtest/compare  — Compare multiple Renko configs side-by-side
  POST /renko/backtest/optimize — Grid-search parameter sweep for Renko pipeline

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

# FastAPI is optional — the module works without it for backtesting
try:
    from fastapi import APIRouter, HTTPException, Depends, Request
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from regime_platform.auth import get_authed_user, require_write, require_admin, TokenData
from regime_platform.middleware.rate_limit import limiter, RATE_LIMIT_WRITE
from regime_platform.services.backtest_cache import backtest_cache
from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline
from renko.brick_engine import BrickDirection

# StreamingResponse for SSE
try:
    from fastapi.responses import StreamingResponse
    STREAMING_AVAILABLE = True
except ImportError:
    STREAMING_AVAILABLE = False

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


# ── Renko Backtest models ──────────────────────────────────────────


class RenkoBacktestRequest(BaseModel):
    """Run a full Renko pipeline backtest with configurable parameters."""
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
    # Optional overrides
    timestamps: Optional[List[float]] = Field(default=None, description="Matching timestamps for prices.")
    regimes: Optional[List[str]] = Field(default=None, description="Pre-computed regime labels aligned with prices.")
    signal_confidence_min: Optional[float] = Field(default=None, ge=0, le=1, description="Min signal confidence to trade.")


class RenkoBacktestResponse(BaseModel):
    """Response from a Renko pipeline backtest."""
    symbol: str
    total_ticks: int
    total_bricks: int
    config_used: Dict[str, Any] = Field(default_factory=dict, description="Config snapshot used for this backtest.")
    stats: Dict[str, Any] = Field(default_factory=dict, description="Comprehensive trading statistics.")
    trades: List[Dict[str, Any]] = Field(default_factory=list, description="List of all trade records.")
    cached: bool = Field(default=False, description="True if result was served from Redis cache (1h TTL).")


class RenkoCompareConfig(BaseModel):
    """A single Renko config to compare."""
    label: str = Field(..., description="Label for this config (e.g. 'aggressive', 'conservative').")
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


class RenkoBacktestCompareRequest(BaseModel):
    """Compare multiple Renko pipeline configurations side-by-side."""
    prices: List[float] = Field(..., min_length=50, description="Historical price series.")
    symbol: str = Field(default="SPY", description="Trading symbol.")
    configs: List[RenkoCompareConfig] = Field(..., min_length=2, max_length=10, description="Configs to compare (2-10).")
    timestamps: Optional[List[float]] = Field(default=None, description="Matching timestamps.")
    regimes: Optional[List[str]] = Field(default=None, description="Pre-computed regime labels.")


class RenkoBacktestCompareResponse(BaseModel):
    """Side-by-side comparison of Renko backtest results."""
    comparisons: List[RenkoBacktestResponse]
    diff: Dict[str, Any] = Field(default_factory=dict, description="Key metric differences between first and best result.")


class RenkoBacktestOptimizeRequest(BaseModel):
    """Grid-search parameter sweep for Renko pipeline optimisation."""
    prices: List[float] = Field(..., min_length=50, description="Historical price series.")
    symbol: str = Field(default="SPY", description="Trading symbol.")
    param_grid: Dict[str, List] = Field(
        ...,
        description="Map of RenkoConfig parameter name to list of values to sweep. "
        "Supported keys: brick_size, sl_bricks, tp_bricks, bull_trigger_n, bear_trigger_n, "
        "trailing_stop, trail_after_bricks, trail_distance_bricks, regime_gate, "
        "max_trades_per_session, max_daily_loss_bricks, max_consecutive_losses.",
    )
    # Fixed params (applied to every run)
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
    # Transaction Costs
    slippage_bps: float = Field(default=2.0, ge=0)
    commission_bps: float = Field(default=5.0, ge=0)
    spread_bps: float = Field(default=1.0, ge=0)


class RenkoBacktestOptimizeResponse(BaseModel):
    """Grid search results: one summary per parameter combination."""
    results: List[Dict[str, Any]] = Field(
        ...,
        description="List of {params, total_pnl_bricks, win_rate, sharpe_estimate, "
        "profit_factor, max_drawdown_bricks, total_trades} for each combination.",
    )
    best_by_sharpe: Optional[Dict[str, Any]] = Field(default=None, description="Params with highest Sharpe estimate.")
    best_by_return: Optional[Dict[str, Any]] = Field(default=None, description="Params with highest total PnL (bricks).")
    n_combinations: int = Field(..., description="Total parameter combinations tested.")


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


# ── Router ─────────────────────────────────────────────────────────

if FASTAPI_AVAILABLE:
    renko_router = APIRouter(prefix="/renko", tags=["renko"])

    @limiter.limit(RATE_LIMIT_WRITE)
    @renko_router.post("/tick")
    async def process_tick(request: Request, body: TickInput, user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Process a single tick through the Renko pipeline."""
        symbol = body.symbol or "SPY"
        pipeline = _get_pipeline(symbol)
        result = pipeline.process_tick(body.price, body.timestamp)

        # ── Fire Discord notifications (non-blocking) ────────────
        _notify_tick_result(symbol, result, pipeline)

        return result

    @limiter.limit(RATE_LIMIT_WRITE)
    @renko_router.post("/tick/batch")
    async def process_batch(request: Request, body: BatchTickInput, symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Process a batch of ticks (backtest / warm-up)."""
        # Body symbol takes precedence over query param
        active_symbol = body.symbol or symbol
        pipeline = _get_pipeline(active_symbol)
        result = pipeline.backtest(
            prices=body.prices,
            timestamps=body.timestamps,
            regimes=body.regimes,
        )

        # ── Fire Discord status notification for batch completion ─
        _notify_batch_complete(active_symbol, result)

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

    # ── Renko Backtest Endpoints (isolated — do not affect live pipeline) ──

    def _build_renko_config(req: "RenkoBacktestRequest") -> "RenkoConfig":
        """Build a RenkoConfig from a backtest request body."""
        return RenkoConfig(
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
            symbol=req.symbol,
        )

    def _run_isolated_backtest(
        prices: List[float],
        config: "RenkoConfig",
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run a backtest in an isolated pipeline (never touches live state)."""
        pipeline = RenkoPipeline(config)
        result = pipeline.backtest(
            prices=prices,
            timestamps=timestamps,
            regimes=regimes,
        )
        return result

    @renko_router.post(
        "/backtest/run",
        response_model=RenkoBacktestResponse,
        summary="Run a full Renko pipeline backtest (isolated from live pipeline)",
    )
    @limiter.limit(RATE_LIMIT_WRITE)
    async def run_renko_backtest(
        request: Request,
        body: RenkoBacktestRequest,
        user: TokenData = Depends(get_authed_user),
    ):
        """
        Run a complete backtest on historical prices using an isolated RenkoPipeline
        instance (never affects the live pipeline).

        Pipeline flow: Tick → Brick Engine → Swing Classifier → Pattern Detector →
        Signal Filter → Risk Manager → Executor → Trade Journal

        Results are cached in Redis (1h TTL) for identical configs — repeated
        requests with the same parameters return instantly from cache.
        The response includes a `cached` field indicating cache HIT/MISS.

        Returns comprehensive stats including win rate, profit factor, Sharpe estimate,
        max drawdown, per-pattern breakdowns, per-regime breakdowns, and Kelly inputs.
        """
        try:
            # ── Check cache first ──────────────────────────────────────
            cache_key_body = body.model_dump()
            cached = await backtest_cache.get("renko", cache_key_body)
            if cached is not None:
                cached["cached"] = True
                return RenkoBacktestResponse(**cached)

            # ── Cache MISS — run the backtest ─────────────────────────
            config = _build_renko_config(body)
            result = await asyncio.to_thread(
                _run_isolated_backtest,
                body.prices,
                config,
                body.timestamps,
                body.regimes,
            )

            stats = result.get("stats", {})
            journal_stats = stats.get("journal", {})
            trades = result.get("trades", [])

            response_data = {
                "symbol": body.symbol,
                "total_ticks": result.get("total_ticks", len(body.prices)),
                "total_bricks": result.get("total_bricks", 0),
                "config_used": config.to_dict(),
                "stats": journal_stats,
                "trades": trades,
                "cached": False,
            }

            # ── Store in cache (fire-and-forget, non-blocking) ────────
            cache_payload = {k: v for k, v in response_data.items() if k != "cached"}
            await backtest_cache.set("renko", cache_key_body, cache_payload)

            return RenkoBacktestResponse(**response_data)

        except Exception as e:
            log.exception(f"Renko backtest run failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── SSE Streaming Backtest Endpoint ─────────────────────────────────────

    if STREAMING_AVAILABLE:

        @renko_router.post(
            "/backtest/run/stream",
            summary="Stream a Renko backtest with progressive chunked results (SSE)",
        )
        @limiter.limit(RATE_LIMIT_WRITE)
        async def run_renko_backtest_stream(
            request: Request,
            body: RenkoBacktestRequest,
            user: TokenData = Depends(get_authed_user),
        ):
            """
            Run a Renko backtest and stream results progressively via Server-Sent Events.

            The pipeline processes prices in chunks of 150 ticks. After each chunk,
            an SSE event is emitted with partial results (trades so far, running stats,
            bricks/ticks processed). The frontend can render a growing equity curve
            and live-updating statistics.

            Event types:
              - ``progress``: Intermediate chunk result (partial trades, stats, percent)
              - ``complete``: Final result (identical to /backtest/run response)
              - ``error``: Pipeline error (if any)

            The final complete result is also cached in Redis (1h TTL), so a subsequent
            non-streaming request for the same config returns instantly from cache.
            """
            import json as _json

            async def event_generator():
                try:
                    # ── Check cache first — if HIT, emit complete immediately ──
                    cache_key_body = body.model_dump()
                    cached = await backtest_cache.get("renko", cache_key_body)
                    if cached is not None:
                        cached["cached"] = True
                        event_data = _json.dumps({"type": "complete", **cached})
                        yield f"data: {event_data}\n\n"
                        return

                    # ── Cache MISS — run chunked backtest ──────────────────
                    config = _build_renko_config(body)

                    def _sync_chunked():
                        """Run chunked backtest in a sync context (for to_thread)."""
                        pipeline = RenkoPipeline(config)
                        results = list(pipeline.backtest_chunked(
                            prices=body.prices,
                            timestamps=body.timestamps,
                            regimes=body.regimes,
                            chunk_size=150,
                        ))
                        return results

                    # Run the full chunked pipeline in a thread (CPU-bound)
                    all_events = await asyncio.to_thread(_sync_chunked)

                    # Emit each event as SSE
                    for event in all_events:
                        yield f"data: {_json.dumps(event, default=str)}\n\n"

                    # ── Cache the final complete result ─────────────────────
                    complete_event = all_events[-1] if all_events else None
                    if complete_event and complete_event.get("type") == "complete":
                        cache_payload = {
                            "symbol": body.symbol,
                            "total_ticks": complete_event.get("total_ticks", len(body.prices)),
                            "total_bricks": complete_event.get("total_bricks", 0),
                            "config_used": config.to_dict(),
                            "stats": complete_event.get("stats", {}).get("journal", {}),
                            "trades": complete_event.get("trades", []),
                        }
                        await backtest_cache.set("renko", cache_key_body, cache_payload)

                except Exception as e:
                    log.exception(f"Renko backtest stream failed: {e}")
                    error_data = _json.dumps({"type": "error", "message": str(e)})
                    yield f"data: {error_data}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # Disable nginx buffering
                },
            )

    @renko_router.post(
        "/backtest/compare",
        response_model=RenkoBacktestCompareResponse,
        summary="Compare multiple Renko pipeline configs side-by-side",
    )
    @limiter.limit(RATE_LIMIT_WRITE)
    async def compare_renko_backtests(
        request: Request,
        body: RenkoBacktestCompareRequest,
        user: TokenData = Depends(get_authed_user),
    ):
        """
        Run multiple Renko pipeline configurations on the same price data
        and return side-by-side results with a diff summary.

        Each config gets its own isolated pipeline instance. The diff
        highlights key metric differences between the first config and
        the best (by Sharpe estimate).

        Results are cached in Redis (1h TTL) for identical compare requests.
        """
        try:
            # ── Check cache ────────────────────────────────────────────
            cache_key_body = body.model_dump()
            cached = await backtest_cache.get("renko:compare", cache_key_body)
            if cached is not None:
                return RenkoBacktestCompareResponse(**cached)

            comparisons = []
            for cfg_input in body.configs:
                config = RenkoConfig(
                    brick_size=cfg_input.brick_size,
                    sl_bricks=cfg_input.sl_bricks,
                    tp_bricks=cfg_input.tp_bricks,
                    trailing_stop=cfg_input.trailing_stop,
                    trail_after_bricks=cfg_input.trail_after_bricks,
                    trail_distance_bricks=cfg_input.trail_distance_bricks,
                    bull_trigger_n=cfg_input.bull_trigger_n,
                    bear_trigger_n=cfg_input.bear_trigger_n,
                    regime_gate=cfg_input.regime_gate,
                    max_trades_per_session=cfg_input.max_trades_per_session,
                    max_daily_loss_bricks=cfg_input.max_daily_loss_bricks,
                    max_consecutive_losses=cfg_input.max_consecutive_losses,
                    slippage_bps=cfg_input.slippage_bps,
                    commission_bps=cfg_input.commission_bps,
                    spread_bps=cfg_input.spread_bps,
                    symbol=body.symbol,
                )

                result = await asyncio.to_thread(
                    _run_isolated_backtest,
                    body.prices,
                    config,
                    body.timestamps,
                    body.regimes,
                )

                stats = result.get("stats", {})
                journal_stats = stats.get("journal", {})

                comparisons.append(RenkoBacktestResponse(
                    symbol=body.symbol,
                    total_ticks=result.get("total_ticks", len(body.prices)),
                    total_bricks=result.get("total_bricks", 0),
                    config_used={**config.to_dict(), "label": cfg_input.label},
                    stats=journal_stats,
                    trades=result.get("trades", []),
                ))

            # Compute diff: first vs best (by Sharpe estimate)
            diff = {}
            if len(comparisons) >= 2:
                first = comparisons[0]
                best = max(
                    comparisons,
                    key=lambda c: c.stats.get("sharpe_estimate", 0.0),
                )
                diff_keys = [
                    "total_trades", "win_rate", "profit_factor",
                    "total_pnl_bricks", "avg_pnl_bricks",
                    "max_drawdown_bricks", "sharpe_estimate",
                ]
                for key in diff_keys:
                    f_val = first.stats.get(key, 0)
                    b_val = best.stats.get(key, 0)
                    diff[key] = {
                        "first": f_val,
                        "best": b_val,
                        "delta": round(b_val - f_val, 6) if isinstance(f_val, (int, float)) else None,
                    }

            response_data = {"comparisons": [c.model_dump() for c in comparisons], "diff": diff}

            # ── Store in cache ──────────────────────────────────────────
            await backtest_cache.set("renko:compare", cache_key_body, response_data)

            return RenkoBacktestCompareResponse(comparisons=comparisons, diff=diff)

        except Exception as e:
            log.exception(f"Renko backtest compare failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @renko_router.post(
        "/backtest/optimize",
        response_model=RenkoBacktestOptimizeResponse,
        summary="Parameter sweep: run Renko backtests across a grid of parameter values",
    )
    @limiter.limit(RATE_LIMIT_WRITE)
    async def optimize_renko_backtest(
        request: Request,
        body: RenkoBacktestOptimizeRequest,
        user: TokenData = Depends(get_authed_user),
    ):
        """
        Grid-search parameter sweep for Renko pipeline optimization.

        Accepts a param_grid dict mapping parameter names to lists of values.
        For each combination, runs a full isolated Renko backtest and returns
        a summary row with key metrics. Useful for finding optimal Renko
        strategy parameters without manual trial-and-error.

        Results are cached in Redis (1h TTL) for identical optimize requests.

        **Warning**: The number of combinations is the product of all grid
        values. Keep grids small (max 50 combinations) to avoid timeouts.
        """
        import itertools as _it

        try:
            # ── Check cache ────────────────────────────────────────────
            cache_key_body = body.model_dump()
            cached = await backtest_cache.get("renko:optimize", cache_key_body)
            if cached is not None:
                return RenkoBacktestOptimizeResponse(**cached)

            # Build parameter combinations from the grid
            grid_keys = list(body.param_grid.keys())
            grid_values = [body.param_grid[k] for k in grid_keys]
            combinations = list(_it.product(*grid_values))

            if len(combinations) > 50:
                raise HTTPException(
                    status_code=400,
                    detail=f"Too many combinations ({len(combinations)}). Max 50 to avoid timeout.",
                )

            # Fixed params from the request
            fixed_params = {
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
                "regime_gate": body.regime_gate,
                "slippage_bps": body.slippage_bps,
                "commission_bps": body.commission_bps,
                "spread_bps": body.spread_bps,
                "symbol": body.symbol,
            }

            results = []
            for combo in combinations:
                # Merge fixed params with grid overrides
                run_params = {**fixed_params}
                for i, key in enumerate(grid_keys):
                    run_params[key] = combo[i]

                config = RenkoConfig(**run_params)

                result = await asyncio.to_thread(
                    _run_isolated_backtest,
                    body.prices,
                    config,
                    body.timestamps,
                    body.regimes,
                )

                journal_stats = result.get("stats", {}).get("journal", {})

                results.append({
                    "params": dict(zip(grid_keys, combo)),
                    "total_pnl_bricks": journal_stats.get("total_pnl_bricks", 0.0),
                    "win_rate": journal_stats.get("win_rate", 0.0),
                    "sharpe_estimate": journal_stats.get("sharpe_estimate", 0.0),
                    "profit_factor": journal_stats.get("profit_factor", 0.0),
                    "max_drawdown_bricks": journal_stats.get("max_drawdown_bricks", 0.0),
                    "total_trades": journal_stats.get("total_trades", 0),
                })

            # Find best combinations
            best_by_sharpe = max(results, key=lambda r: r["sharpe_estimate"]) if results else None
            best_by_return = max(results, key=lambda r: r["total_pnl_bricks"]) if results else None

            response_data = {
                "results": results,
                "best_by_sharpe": best_by_sharpe,
                "best_by_return": best_by_return,
                "n_combinations": len(combinations),
            }

            # ── Store in cache ──────────────────────────────────────────
            await backtest_cache.set("renko:optimize", cache_key_body, response_data)

            return RenkoBacktestOptimizeResponse(**response_data)

        except HTTPException:
            raise
        except Exception as e:
            log.exception(f"Renko backtest optimize failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

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
