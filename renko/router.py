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
    from fastapi import APIRouter, HTTPException, Depends
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from regime_platform.auth import get_authed_user, require_write, require_admin, TokenData
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

    @renko_router.post("/tick")
    async def process_tick(body: TickInput, user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
        """Process a single tick through the Renko pipeline."""
        symbol = body.symbol or "SPY"
        pipeline = _get_pipeline(symbol)
        result = pipeline.process_tick(body.price, body.timestamp)

        # ── Fire Discord notifications (non-blocking) ────────────
        _notify_tick_result(symbol, result, pipeline)

        return result

    @renko_router.post("/tick/batch")
    async def process_batch(body: BatchTickInput, symbol: str = "SPY", user: TokenData = Depends(get_authed_user)) -> Dict[str, Any]:
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

    @renko_router.post("/reset")
    async def reset_pipeline(symbol: str = "SPY", user: TokenData = Depends(require_admin)) -> Dict:
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
